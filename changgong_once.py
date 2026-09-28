"""本次长工 300 张卡的一次性采集；只使用已观察到的网站接口。"""
import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import qrcode
import requests

from nexsim_batch import atomic_write, exclusive_lock, now, export_excel

BASE = 'https://admin.nexsimus.com'
OUT = Path('C:/Users/27792/Desktop/esim二维码/changgong')
PRIVATE = OUT / '.nexsim-live'
ORG = 639
PRODUCT_ID = 7
TARGET = 300


class Live:
    def __init__(self):
        lines = Path('C:/Users/27792/Desktop/长工.txt').read_text(encoding='utf-8-sig').strip().splitlines()
        if len(lines) != 2:
            raise RuntimeError('登录文件格式不符')
        self.session = requests.Session()
        response = self.session.post(BASE + '/api/auth/login', json={'username': lines[0].strip(), 'password': lines[1].strip()}, timeout=(10, 30), allow_redirects=False)
        lines = None
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 0 or payload['data']['user']['orgId'] != ORG:
            raise RuntimeError('登录失败或组织不匹配')
        self.session.headers['Authorization'] = 'Bearer ' + payload['data']['token']
        self.session.headers['Content-Type'] = 'application/json'

    def get(self, endpoint, params=None):
        response = self.session.get(BASE + endpoint, params=params, timeout=(10, 60), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError(f'GET HTTP {response.status_code}')
        payload = response.json()
        if payload.get('code') != 0:
            raise RuntimeError('业务查询失败，停止')
        return payload['data']


def eligible(client):
    # The UI's activation-options endpoint caps results at 100, even with limit=500.
    # Walk the observed inventory page API and keep the exact selected product.
    rows = []
    page_no = 1
    while True:
        page = client.get('/api/inventory/page', {'status': 'ALLOCATED', 'simType': 'ESIM',
                         'iccid': '', 'dateType': 'IMPORTED', 'pageNo': page_no, 'pageSize': 100})
        rows.extend(r for r in page['records'] if r['ownerOrgId'] == ORG and r.get('productId') == PRODUCT_ID)
        if page_no >= page['pages']:
            break
        page_no += 1
    for row in rows:
        if row['ownerOrgId'] != ORG or row['inventoryType'] != 'P' or row['simType'] != 'ESIM' \
                or row['status'] != 'ALLOCATED' or not row.get('qrCodeSupported') \
                or not isinstance(row['iccid'], str) or not row['iccid'].isdigit():
            raise RuntimeError('库存包含不符合条件的卡，停止')
    if len({r['iccid'] for r in rows}) != len(rows):
        raise RuntimeError('库存 ICCID 重复')
    return rows


def collect(client):
    PRIVATE.mkdir(parents=True, exist_ok=True)
    manifest = PRIVATE / 'cards.json'
    if manifest.exists():
        rows = json.loads(manifest.read_text(encoding='utf-8'))
    else:
        rows = [r for r in eligible(client) if r.get('qrViewCount') == 0]
        if not rows:
            raise RuntimeError('没有可开卡库存')
        rows = rows[:TARGET]
        atomic_write(manifest, json.dumps(rows, ensure_ascii=False, indent=2).encode('utf-8'))
    if not 1 <= len(rows) <= TARGET:
        raise RuntimeError('固定清单数量错误')
    with Path('C:/Users/27792/Desktop/esim二维码/开户激活模板.csv').open(encoding='utf-8-sig', newline='') as f:
        if next(csv.reader(f), None) != ['ICCID']:
            raise RuntimeError('模板表头改变')
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['ICCID'])
    writer.writerows([[r['iccid']] for r in rows])
    atomic_write(OUT / f'开户激活_{len(rows)}.csv', out.getvalue().encode('utf-8-sig'))
    for i, row in enumerate(rows, 1):
        path = OUT / f"eSIM二维码_{row['iccid']}.png"
        if row.get('qr_sha256'):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != row['qr_sha256']:
                raise RuntimeError('已保存二维码丢失或改变')
            continue
        data = client.get(f"/api/inventory/{row['id']}/qr-code")
        if data.get('cardId') != row['id'] or data.get('iccid') != row['iccid'] \
                or data.get('status') != 'READY' or not data.get('activationCode', '').startswith('LPA:1$'):
            raise RuntimeError(f'第 {i} 张二维码不可用，停止')
        buffer = io.BytesIO()
        qrcode.make(data['activationCode'], box_size=10, border=4).save(buffer, format='PNG')
        blob = buffer.getvalue()
        if path.exists() and path.read_bytes() != blob:
            raise RuntimeError('已有同名二维码且内容不同，停止')
        atomic_write(path, blob)
        row['qr_sha256'] = hashlib.sha256(blob).hexdigest()
        row['qr_saved_at'] = now()
        atomic_write(manifest, json.dumps(rows, ensure_ascii=False, indent=2).encode('utf-8'))
        if i % 10 == 0 or i == len(rows):
            print(f'已保存二维码 {i}/{len(rows)}', flush=True)
        time.sleep(0.2)
    report_rows = [dict(iccid=r['iccid'], phase='prepared', order_no=None, request_id='', verified_at=None,
                        note='已采集二维码，等待激活') for r in rows]
    report_dir = OUT / '.nexsim-batch'
    report_dir.mkdir(exist_ok=True)
    atomic_write(report_dir / 'report.json', json.dumps(report_rows, ensure_ascii=False).encode('utf-8'))
    export_excel({'output_dir': str(OUT)})
    print(f'采集完成：{len(rows)} 张二维码 + CSV + 文本格式 Excel；距离 {TARGET} 张差 {TARGET-len(rows)} 张；尚未开卡。', flush=True)


def all_pages(client, endpoint):
    first = client.get(endpoint, {'pageNo': 1, 'pageSize': 100})
    rows = first['records']
    for page in range(2, first['pages'] + 1):
        rows.extend(client.get(endpoint, {'pageNo': page, 'pageSize': 100})['records'])
    return rows


def saved_cards():
    rows = json.loads((PRIVATE / 'cards.json').read_text(encoding='utf-8'))
    if not rows or len({r['iccid'] for r in rows}) != len(rows):
        raise RuntimeError('清单为空或重复')
    for row in rows:
        path = OUT / f"eSIM二维码_{row['iccid']}.png"
        if hashlib.sha256(path.read_bytes()).hexdigest() != row.get('qr_sha256'):
            raise RuntimeError('二维码文件不完整或与清单不一致')
    return rows


def verify(client):
    cards = saved_cards()
    orders = all_pages(client, '/api/orders/page')
    subscribers = all_pages(client, '/api/orders/subscribers/page')
    expected = {}
    batch_file = PRIVATE / 'activation-response.json'
    if batch_file.exists():
        data = json.loads(batch_file.read_text(encoding='utf-8'))['data']
        expected = {r['iccid']: r.get('orderId') for r in data.get('results', [])}
    result = []
    proofs = []
    for card in cards:
        matched = [o for o in orders if o.get('iccid') == card['iccid'] and o.get('cardId') == card['id']
                   and o.get('orgId') == ORG and o.get('productId') == PRODUCT_ID
                   and (not expected or o.get('id') == expected.get(card['iccid']))]
        order = max(matched, key=lambda o: o['id']) if matched else {}
        subs = [s for s in subscribers if s.get('iccid') == card['iccid'] and s.get('cardId') == card['id']
                and s.get('orgId') == ORG and s.get('productId') == PRODUCT_ID
                and s.get('orderId') == order.get('id')]
        sub = subs[0] if len(subs) == 1 else {}
        success = bool(order.get('orderStatus') == 'ACTIVE' and sub.get('subscriberStatus') == 'ACTIVE'
                       and sub.get('signalAddonStatus') == 'SUCCESS' and order.get('signalAddonStatus') == 'SUCCESS'
                       and sub.get('phoneNumber') and sub.get('phoneNumber') == order.get('phoneNumber'))
        stamp = now()
        result.append(dict(iccid=card['iccid'], phase='verified' if success else 'unconfirmed',
                           order_no=order.get('orderNo', ''), request_id='', verified_at=stamp if success else None,
                           phone_number=sub.get('phoneNumber', ''), order_status=order.get('orderStatus', ''),
                           subscriber_status=sub.get('subscriberStatus', ''), signal_status=sub.get('signalAddonStatus', ''),
                           note='订单、号码、信号和二维码已逐项核对' if success else
                           order.get('failureReason') or ('开户成功，信号仍在开通中' if order.get('orderStatus') == 'ACTIVE' else '未满足全部成功条件，不计入成功')))
        proofs.append({'iccid': card['iccid'], 'checked_at': stamp, 'order': order, 'subscriber': sub})
    atomic_write(PRIVATE / 'verification-evidence.json', json.dumps(proofs, ensure_ascii=False, indent=2).encode('utf-8'))
    atomic_write(OUT / '.nexsim-batch' / 'report.json', json.dumps(result, ensure_ascii=False).encode('utf-8'))
    table = io.StringIO(newline='')
    writer = csv.DictWriter(table, fieldnames=list(result[0]))
    writer.writeheader()
    writer.writerows(result)
    atomic_write(OUT / '激活结果.csv', table.getvalue().encode('utf-8-sig'))
    export_excel({'output_dir': str(OUT)})
    confirmed = sum(r['phase'] == 'verified' for r in result)
    print(f'本次实时确认成功 {confirmed}/{len(cards)}；目标 {TARGET}；核对时间 {now()}', flush=True)
    return confirmed == len(cards)


def activate(client, count, max_total):
    cards = saved_cards()
    if len(cards) != count:
        raise RuntimeError('授权数量与固定清单不一致，停止')
    marker = PRIVATE / 'activation-intent.json'
    response_file = PRIVATE / 'activation-response.json'
    if not marker.exists():
        current = {r['iccid']: r['id'] for r in eligible(client)}
        if any(current.get(r['iccid']) != r['id'] for r in cards):
            raise RuntimeError('部分卡已不再可开卡，停止提交')
        iccids = {r['iccid'] for r in cards}
        if any(o.get('iccid') in iccids for o in all_pages(client, '/api/orders/page')):
            raise RuntimeError('本批卡已有订单，停止以避免重复开卡')
        product = next(p for p in client.get('/api/products', {'orgId': ORG, 'operationType': 'ACTIVATION'}) if p['id'] == PRODUCT_ID)
        price = Decimal(str(product['displayPrice']))
        total = price * len(cards)
        balances = client.get('/api/finance/organizations/639/balances')
        balance = Decimal(str(next(b['balance'] for b in balances if b['balanceType'] == 'ACTIVATION')))
        if product['productCode'] != 'P_VOICE_SMS_30M_100' or total > Decimal(max_total) or total > balance:
            raise RuntimeError('套餐、授权金额上限或开卡余额不符合要求')
        csv_path = OUT / f'开户激活_{len(cards)}.csv'
        with csv_path.open(encoding='utf-8-sig', newline='') as f:
            imported = list(csv.DictReader(f))
        if [r['ICCID'] for r in imported] != [r['iccid'] for r in cards]:
            raise RuntimeError('CSV 与已保存二维码的清单不一致')
        atomic_write(marker, json.dumps({'time': now(), 'count': len(cards), 'unit_price': str(price),
                                         'total': str(total), 'status': 'submission_attempted'}, ensure_ascii=False).encode('utf-8'))
        # Native UI CSV contract; no automatic retry if the response is lost.
        client.session.headers.pop('Content-Type', None)
        response = client.session.post(BASE + '/api/orders/batch-create-and-activate-csv',
            files={'file': (csv_path.name, csv_path.read_bytes(), 'text/csv'),
                   'request': ('blob', json.dumps({'orgId': ORG, 'productId': PRODUCT_ID}), 'application/json')},
            timeout=(10, 120), allow_redirects=False)
        payload = response.json()
        atomic_write(response_file, json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
        if response.status_code != 200 or payload.get('code') != 0:
            raise RuntimeError('提交响应异常；已保存回包，仅允许回查，不重发')
    if not response_file.exists():
        verify(client)
        raise RuntimeError('之前提交的结果未知，禁止重发；请平台核对原批次')
    response = json.loads(response_file.read_text(encoding='utf-8'))
    if response.get('code') != 0 or not isinstance(response.get('data'), dict):
        raise RuntimeError('之前提交未得到有效结果；请人工核对，不重发')
    job = response['data'].get('job')
    if job:
        for _ in range(360):
            status = client.get(f"/api/orders/batch-jobs/{job['id']}")
            atomic_write(PRIVATE / 'job-status.json', json.dumps(status, ensure_ascii=False, indent=2).encode('utf-8'))
            print(f"平台处理中：成功 {status.get('successCount', 0)}，失败 {status.get('failedCount', 0)}，状态 {status.get('jobStatus')}", flush=True)
            if status.get('jobStatus') not in {'PENDING', 'PROCESSING'}:
                break
            time.sleep(5)
    if not verify(client):
        raise RuntimeError('部分卡未确认成功，查看激活核对表；不要重新提交本批次')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'collect', 'activate', 'verify'])
    parser.add_argument('--count', type=int)
    parser.add_argument('--max-total')
    args = parser.parse_args()
    client = Live()
    if args.action == 'inspect':
        rows = eligible(client)
        products = client.get('/api/products', {'orgId': ORG, 'operationType': 'ACTIVATION'})
        product = next(p for p in products if p['id'] == PRODUCT_ID)
        print(json.dumps({'eligible_count': len(rows), 'product': product['productCode'],
                          'unit_price': product.get('displayPrice'), 'target': TARGET,
                          'qr_status_counts': {s: sum(r.get('qrViewStatus') == s for r in rows)
                                               for s in set(r.get('qrViewStatus') for r in rows)}}, ensure_ascii=False))
    else:
        PRIVATE.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(PRIVATE / 'run.lock'):
            if args.action == 'collect':
                if (PRIVATE / 'activation-intent.json').exists():
                    raise RuntimeError('已进入激活流程，请用 verify 回查，不再重新采集')
                collect(client)
            elif args.action == 'verify':
                if not verify(client):
                    raise SystemExit(2)
            else:
                if not args.count or not args.max_total:
                    raise RuntimeError('需要明确 --count 和 --max-total 才能提交扣费激活')
                activate(client, args.count, args.max_total)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('停止：' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        raise SystemExit(2)
