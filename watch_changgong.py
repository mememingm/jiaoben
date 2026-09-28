"""This batch only: wait for platform signal provisioning, using read-only queries."""
import json
import time
from collections import Counter
import changgong_once as c

client = c.Live()
ids = {r['iccid'] for r in c.saved_cards()}
assert len(ids) == 300
for attempt in range(40):
    orders = [r for r in c.all_pages(client, '/api/orders/page') if r.get('iccid') in ids]
    subscribers = [r for r in c.all_pages(client, '/api/orders/subscribers/page') if r.get('iccid') in ids]
    summary = {'checked_at': c.now(), 'orders': dict(Counter(r.get('orderStatus') for r in orders)),
               'signals': dict(Counter(r.get('signalAddonStatus') for r in subscribers))}
    c.atomic_write(c.PRIVATE / 'live-progress.json', json.dumps(summary, ensure_ascii=False, indent=2).encode('utf-8'))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if len(subscribers) >= 298 and all(r.get('signalAddonStatus') == 'SUCCESS' for r in subscribers):
        break
    time.sleep(20)
c.verify(client)
balances = client.get('/api/finance/organizations/639/balances')
c.atomic_write(c.PRIVATE / 'balance-after.json', json.dumps({'checked_at': c.now(), 'balances': balances}, ensure_ascii=False, indent=2).encode('utf-8'))
