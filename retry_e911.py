"""Only retry original orders explicitly rejected for E911; never retry unknown results."""
import json
from oneoff import Live, PRIVATE, saved_cards, all_pages, atomic_write, now, BASE, ORG, PRODUCT_ID, exclusive_lock

with exclusive_lock(PRIVATE / 'run.lock'):
    client = Live()
    cards = {c['id']: c['iccid'] for c in saved_cards()}
    original = json.loads((PRIVATE / 'activation-response.json').read_text(encoding='utf-8'))
    ids = {r['orderId'] for r in original['data']['results'] if r.get('success')}
    orders = [o for o in all_pages(client, '/api/orders/page') if o['id'] in ids]
    eligible = [o for o in orders if o.get('orderStatus') == 'FAILED' and 'E911' in o.get('failureReason', '')
                and o.get('addressPoolId') and o.get('orgId') == ORG and o.get('productId') == PRODUCT_ID
                and cards.get(o.get('cardId')) == o.get('iccid')]
    print(f'原订单地址失败可重试数量：{len(eligible)}', flush=True)
    for order in eligible:
        marker = PRIVATE / f"retry-e911-{order['id']}.json"
        if marker.exists():
            print('该原订单已有重试记录，仅回查，不重发', flush=True)
            continue
        atomic_write(marker, json.dumps({'time':now(), 'orderId':order['id'], 'before':order,
                                         'status':'retry_attempted'},ensure_ascii=False).encode('utf-8'))
        try:
            r = client.session.post(BASE+f"/api/orders/{order['id']}/activate",timeout=(10,90),allow_redirects=False)
            payload = r.json()
            atomic_write(marker.with_suffix('.response.json'), json.dumps(payload,ensure_ascii=False).encode('utf-8'))
            print(f"原订单 {order['id']} 返回 HTTP {r.status_code}，code={payload.get('code')}，后续以查询为准",flush=True)
        except Exception as exc:
            print(f"原订单 {order['id']} 结果待确认：{type(exc).__name__}；不重发",flush=True)
