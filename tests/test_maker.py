import asyncio
from dataclasses import replace
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from test_app import SETTINGS, AUTH, BUY_AUTH, IDEMPOTENCY_KEY


class Exchange:
    def __init__(self):
        self.orders = {}
        self.submissions = []
        self.ask = Decimal('2000000.01')
        self.fee = '0.4'
        self.taker = '0.6'
        self.available = '100000'
        self.minimum = '0.0002'
        self.timeout_submit = False
        self.timeout_cancel = False
        self.cancel_fills = False
        self.hide_lookup = False
        self.reject = False
        self.paths = []

    def response(self, data):
        return httpx.Response(200, json={'error': False, 'data': data})

    def fill(self, order_id, quantity=None, status='FILLED'):
        order = self.orders[order_id]
        qty = Decimal(quantity or order['amount'])
        price = Decimal(order['price'])
        order.update(status=status, cumulativeAmount=str(qty), trades=[{
            'transactionId': order_id, 'orderId': order_id, 'currencyPair': 'BTC_CZK',
            'amount': str(qty), 'price': str(price), 'fee': str(qty * price * Decimal('.004')),
            'feeType': 'MAKER', 'createdTimestamp': 1780000000000,
        }])

    def __call__(self, request):
        path = request.url.path.rsplit('/', 1)[-1]
        self.paths.append(path)
        form = {k: v[0] for k,v in parse_qs(request.content.decode()).items()}
        if path == 'tradingPairs':
            assert request.method == 'GET'
            return self.response([{'name':'BTC_CZK', 'priceDecimals':2, 'lotDecimals':8, 'minAmount':self.minimum}])
        if path == 'traderFees': return self.response({'maker':self.fee, 'taker':self.taker})
        if path == 'orderBook':
            assert request.url.params['currencyPair'] == 'BTC_CZK'
            assert request.url.params['groupByPriceLimit'] == 'true'
            return self.response({'asks':[{'price':str(self.ask),'amount':'1'}],
                                  'bids':[{'price':str(self.ask - Decimal('0.02')),'amount':'1'}],
                                  'status':'TRADING'})
        if path == 'balances':
            return self.response({'CZK': {'available': self.available}})
        if path == 'buyLimit':
            assert form['postOnly'] == '1' and form['immediateOrCancel'] == '0'
            assert Decimal(form['price']) < self.ask
            assert form['clientOrderId'].isdigit()
            self.submissions.append(form)
            if self.reject: return httpx.Response(200, json={'error':True, 'errorMessage':'post only rejected'})
            order_id = len(self.orders) + 1
            self.orders[order_id] = {**form, 'id':order_id, 'type':'BUY', 'status':'OPEN', 'cumulativeAmount':'0', 'trades':[]}
            if self.timeout_submit: raise httpx.ReadTimeout('lost response', request=request)
            return self.response(order_id)
        if path == 'order':
            return self.response([] if self.hide_lookup else [o for o in self.orders.values() if o['clientOrderId']==form['clientOrderId']])
        if path == 'orderById': return self.response(self.orders[int(form['orderId'])])
        if path == 'cancelOrder':
            order_id = int(form['orderId'])
            if self.cancel_fills: self.fill(order_id)
            else: self.orders[order_id]['status'] = 'CANCELLED'
            if self.timeout_cancel: raise httpx.ReadTimeout('lost cancel response', request=request)
            return self.response(True)
        raise AssertionError(f'Unexpected endpoint: {path}')


def tick(client, app):
    client.portal.call(app.state.purchases.tick)


def age(app):
    state = app.state.purchase_store.maker_state(IDEMPOTENCY_KEY)
    state['attempts'][-1]['placed_at'] = 0
    app.state.purchase_store.save_maker(IDEMPOTENCY_KEY, state)


def start(exchange, tmp_path=None):
    settings = replace(SETTINGS, purchase_poll_seconds=3600,
        database_path=str(tmp_path/'controller.db') if tmp_path else ':memory:')
    return create_app(settings, httpx.MockTransport(exchange))


def test_budget_fee_post_only_fill_and_idempotency():
    exchange = Exchange(); app = start(exchange)
    with TestClient(app) as client:
        first = client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000})
        assert first.status_code == 202
        tick(client,app)
        form = exchange.submissions[0]
        assert Decimal(form['price']) == Decimal('2000000')
        assert Decimal(form['price'])*Decimal(form['amount'])*Decimal('1.004') <= 1000
        exchange.fill(1)
        tick(client,app)
        result = client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}',headers=AUTH).json()
        assert result['success'] and not result['pending']
        assert Decimal(str(result['btc_bought'])) == Decimal(form['amount'])
        assert 997 < result['spent_czk'] <= 1000
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000})
        assert len(exchange.submissions)==1
        assert client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1001}).status_code==409
        assert 'buyInstant' not in exchange.paths


def test_partial_fill_cancel_reprice_upward_only_remaining_budget():
    exchange=Exchange(); app=start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':2000})
        tick(client,app)
        exchange.fill(1,'0.0003','PARTIALLY_FILLED')
        age(app)
        tick(client,app)
        assert len(exchange.submissions)==1  # cancellation alone never places the next order
        exchange.ask=Decimal('2100000.01')
        tick(client,app)
        assert len(exchange.submissions)==2
        next_order=exchange.submissions[1]
        assert Decimal(next_order['price'])==2100000
        assert Decimal(next_order['amount'])*2100000*Decimal('1.004')+Decimal('602.4')<=2000
        exchange.fill(2)
        tick(client,app)
        result=client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}',headers=AUTH).json()
        assert result['success'] and result['spent_czk']<=2000
        assert Decimal(str(result['btc_bought']))==Decimal('.0003')+Decimal(next_order['amount'])


def test_cancelled_unfilled_order_with_null_cumulative_amount_is_replaced():
    exchange = Exchange(); app = start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount': 1000})
        tick(client, app)
        age(app)
        tick(client, app)
        assert exchange.orders[1]['status'] == 'CANCELLED'
        exchange.orders[1]['cumulativeAmount'] = None
        tick(client, app)
        assert len(exchange.submissions) == 2
        state = app.state.purchase_store.maker_state(IDEMPOTENCY_KEY)
        assert state['attempts'][0]['status'] == 'CLOSED'
        assert state['attempts'][0]['btc_bought'] == '0'


@pytest.mark.parametrize('fill_during_cancel',[False,True])
def test_cancel_timeout_and_restart_does_not_double_buy(tmp_path,fill_during_cancel):
    exchange=Exchange(); app=start(exchange,tmp_path)
    with TestClient(app) as client:
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000})
        tick(client,app); age(app)
        exchange.timeout_cancel=True; exchange.cancel_fills=fill_during_cancel
        tick(client,app)
        assert len(exchange.submissions)==1
    app=start(exchange,tmp_path)
    with TestClient(app) as client:
        tick(client,app)
        assert len(exchange.submissions)==(1 if fill_during_cancel else 2)


def test_unknown_submission_waits_and_recovers_after_restart(tmp_path):
    exchange=Exchange(); exchange.timeout_submit=True; exchange.hide_lookup=True
    app=start(exchange,tmp_path)
    with TestClient(app) as client:
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000})
        tick(client,app); tick(client,app)
        assert len(exchange.submissions)==1
        assert client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}',headers=AUTH).json()['pending']
    exchange.hide_lookup=False
    exchange.fill(1)
    app=start(exchange,tmp_path)
    with TestClient(app) as client:
        tick(client,app)
        assert client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}',headers=AUTH).json()['success']
        assert len(exchange.submissions)==1


def test_fee_cap_blocks_submission_and_can_resume():
    exchange=Exchange(); exchange.fee='0.5'; app=start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000})
        tick(client,app)
        result=client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}',headers=AUTH).json()
        assert result['pending'] and '0.4%' in result['detail']
        assert not exchange.submissions
        exchange.fee='0.4'; tick(client,app)
        assert len(exchange.submissions)==1


def test_background_worker_progresses_without_browser_polling():
    exchange=Exchange()
    app=create_app(replace(SETTINGS,purchase_poll_seconds=.01),httpx.MockTransport(exchange))
    with TestClient(app) as client:
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000})
        async def wait_for_submit():
            for _ in range(100):
                if exchange.submissions: return
                await asyncio.sleep(.01)
            raise AssertionError('Worker did not submit')
        client.portal.call(wait_for_submit)
        exchange.fill(1)
        async def wait_for_completion():
            for _ in range(100):
                if app.state.purchase_store.get(IDEMPOTENCY_KEY).status=='FILLED': return
                await asyncio.sleep(.01)
            raise AssertionError('Worker did not finish')
        client.portal.call(wait_for_completion)


def test_incomplete_fills_prevent_replacement():
    exchange=Exchange(); app=start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':2000});tick(client,app)
        exchange.fill(1,'.0003','CANCELLED');exchange.orders[1]['trades']=[]
        tick(client,app)
        assert len(exchange.submissions)==1
        assert client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}',headers=AUTH).json()['pending']


def test_requirements_returns_live_minimum_and_low_budget_never_submits():
    exchange=Exchange(); app=start(exchange)
    with TestClient(app) as client:
        minimum=client.get('/buy_bitcoin/requirements',headers=AUTH)
        assert minimum.status_code==200
        assert minimum.json()=={'min_amount_btc':.0002,'min_amount_czk':402.41,'max_amount_czk':5000}
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':25});tick(client,app)
        assert not exchange.submissions
        result=client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}',headers=AUTH).json()
        assert not result['success'] and result['status']=='rejected'


def test_post_only_rejection_is_paced_and_retry_has_new_client_id():
    exchange=Exchange(); exchange.reject=True; app=start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000});tick(client,app);tick(client,app)
        assert len(exchange.submissions)==1
        state=app.state.purchase_store.maker_state(IDEMPOTENCY_KEY);state['next_attempt_at']=0
        app.state.purchase_store.save_maker(IDEMPOTENCY_KEY,state)
        exchange.reject=False;tick(client,app)
        assert len(exchange.submissions)==2
        assert exchange.submissions[0]['clientOrderId']!=exchange.submissions[1]['clientOrderId']


def test_own_fills_do_not_hide_or_fake_deposits():
    exchange=Exchange()
    balance=Decimal('2000')
    def transport(request):
        if request.url.path.endswith('/balances'):
            return exchange.response({'CZK':{'balance':str(balance), 'available':str(balance)}})
        return exchange(request)
    app=create_app(replace(SETTINGS,purchase_poll_seconds=3600),httpx.MockTransport(transport))
    with TestClient(app) as client:
        assert client.get('/funding_balance/czk',headers=AUTH).json()==2000
        client.post('/buy_bitcoin',headers=BUY_AUTH,json={'amount':1000});tick(client,app)
        exchange.fill(1)
        order=exchange.orders[1]
        spent=Decimal(order['amount'])*Decimal(order['price'])*Decimal('1.004')
        balance-=spent
        # Even before the worker's next tick, reconciliation adds back only actual debits.
        assert client.get('/funding_balance/czk',headers=AUTH).json()==2000
        balance+=1000
        assert client.get('/funding_balance/czk',headers=AUTH).json()==3000
        tick(client,app)
        assert len(exchange.submissions)==1


def test_small_budget_reserves_taker_fee_and_rounding():
    exchange = Exchange()
    exchange.ask = Decimal('1813770.01')
    exchange.minimum = '0.00001'
    exchange.available = '75.78340959'
    app = start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':75.78})
        tick(client, app)
        form = exchange.submissions[0]
        reserved = Decimal(form['price']) * Decimal(form['amount']) * Decimal('1.006') + Decimal('.01')
        assert reserved <= Decimal('75.78')
        assert form['amount'] == '0.00004152'


def test_unavailable_balance_waits_without_rejecting_or_submitting():
    exchange = Exchange(); exchange.available = '0'; app = start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':1000})
        tick(client, app)
        result = client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}', headers=AUTH).json()
        assert result['pending'] and result['detail']
        assert not exchange.submissions
        exchange.available = '500'
        tick(client, app)
        form = exchange.submissions[0]
        assert Decimal(form['price']) * Decimal(form['amount']) * Decimal('1.006') + Decimal('.01') <= 500


@pytest.mark.parametrize('taker', ['-1', 'NaN', '100', None])
def test_invalid_reservation_fee_never_submits(taker):
    exchange = Exchange(); exchange.taker = taker; app = start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':1000})
        tick(client, app)
        assert not exchange.submissions


def test_rejection_detail_survives_retry_wait():
    exchange = Exchange(); exchange.reject = True; app = start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':1000})
        tick(client, app); tick(client, app)
        result = client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}', headers=AUTH).json()
        assert result['detail'] == 'post only rejected'


@pytest.mark.parametrize('submitted', [False, True])
def test_cancel_prevents_new_orders_and_post_replay(submitted):
    exchange = Exchange(); exchange.reject = True; app = start(exchange)
    url = f'/buy_bitcoin/{IDEMPOTENCY_KEY}/cancel'
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':1000})
        if submitted:
            tick(client, app)
        assert client.post(url).status_code == 401
        assert client.post(url, headers=AUTH).status_code == 202
        tick(client, app)
        result = client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':1000}).json()
        assert result['status'] == 'cancelled' and not result['pending']
        assert client.post(url, headers=AUTH).status_code == 200
        tick(client, app)
        assert len(exchange.submissions) == int(submitted)


@pytest.mark.parametrize('fill_during_cancel', [False, True])
def test_requested_cancel_reconciles_fills_after_timeout_and_restart(tmp_path, fill_during_cancel):
    exchange = Exchange(); app = start(exchange, tmp_path)
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':2000})
        tick(client, app)
        exchange.fill(1, '.0003', 'PARTIALLY_FILLED')
        exchange.timeout_cancel = True
        exchange.cancel_fills = fill_during_cancel
        client.post(f'/buy_bitcoin/{IDEMPOTENCY_KEY}/cancel', headers=AUTH)
        tick(client, app)
    app = start(exchange, tmp_path)
    with TestClient(app) as client:
        tick(client, app)
        result = client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}', headers=AUTH).json()
        assert result['status'] == 'cancelled' and not result['pending']
        assert result['btc_bought'] > 0 and result['spent_czk'] > 0
        assert result['completed_at'] == 1780000000
        assert len(exchange.submissions) == 1


def test_cancel_unknown_submit_does_not_assume_empty_lookup_means_rejection():
    exchange = Exchange(); exchange.timeout_submit = True; exchange.hide_lookup = True
    app = start(exchange)
    with TestClient(app) as client:
        client.post('/buy_bitcoin', headers=BUY_AUTH, json={'amount':1000})
        tick(client, app)
        client.post(f'/buy_bitcoin/{IDEMPOTENCY_KEY}/cancel', headers=AUTH)
        tick(client, app)
        result = client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}', headers=AUTH).json()
        assert result['pending'] and result['detail']
        exchange.hide_lookup = False
        tick(client, app); tick(client, app)
        result = client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}', headers=AUTH).json()
        assert not result['pending'] and result['status'] == 'cancelled'
        assert len(exchange.submissions) == 1


def test_offline_cancellation_is_durable_before_worker_starts(tmp_path, monkeypatch, capsys):
    from app.cancel_purchase import main
    from app.purchase_store import PurchaseStore
    from app.maker import MakerPurchases
    import sys

    path = tmp_path / 'controller.db'
    store = PurchaseStore(str(path))
    store.create_or_get(IDEMPOTENCY_KEY, '1000.00', '123', MakerPurchases.initial_state())
    store.close()
    monkeypatch.setenv('DATABASE_PATH', str(path))
    monkeypatch.setattr(sys, 'argv', ['cancel_purchase', IDEMPOTENCY_KEY])
    main()
    assert 'CANCELLING' in capsys.readouterr().out
    exchange = Exchange(); app = start(exchange, tmp_path)
    with TestClient(app) as client:
        tick(client, app)
        result = client.get(f'/buy_bitcoin/{IDEMPOTENCY_KEY}', headers=AUTH).json()
        assert result['status'] == 'cancelled' and not exchange.submissions
