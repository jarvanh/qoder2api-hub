# -*- coding: utf-8 -*-
"""只含共享池的 quota 响应应可读取、刷新和持久化，不要求个人额度。"""
import copy
import json
import os
import socket
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import bootstrap, check, declare_suite, finish

TMP = tempfile.TemporaryDirectory(prefix='shared-quota-only-')
os.environ['ACCOUNTS_DIR'] = os.path.join(TMP.name, 'accounts')
os.environ['QD_PROXY_USAGE_DIR'] = os.path.join(TMP.name, 'usage')
os.environ['QD_NATIVE_IDENTITY'] = '0'
P, A = bootstrap()
declare_suite(__file__, ['qoder_accounts.py'])

URL = A.get_realm_config('intl')['openapi'] + A.PATH_QUOTA
SOURCE = {'host': A.get_realm_config('intl')['openapi'], 'path': A.PATH_QUOTA}
OLD = {'remain': 0, 'size': 0, 'used': 0, 'remain_effective': 6000,
       'shared': {'size': 6000, 'used': 0, 'remain': 6000}, 'updated_at': 1}


def payload(used=254, remain=5746, cap=6000, available=True, exceeded=False):
    return {'orgResourcePackage': {'used': used, 'remaining': remain, 'cap': cap,
                                   'available': available},
            'isQuotaExceeded': exceeded, 'totalUsagePercentage': 0.0}


def account(uid='quota-test', credits=None, path=None):
    return A.Account({'uid': uid, 'realm': 'intl', 'accessToken': 'fake-token',
                      'credits': copy.deepcopy(credits)}, path=path)


network_attempts = []
def no_network(*args, **kwargs):
    network_attempts.append(True)
    raise AssertionError('离线测试禁止真实出网')

with mock.patch.object(socket.socket, 'connect', no_network):
    for used, remain, cap, old in ((254, 5746, 6000, OLD), (3001, 6999, 10000, None)):
        for source in (False, True):
            acc = account(credits=old)
            with mock.patch.object(A, 'http_json', return_value=payload(used, remain, cap)) as request:
                result = acc._fetch_credits_via_source(None, SOURCE) if source else acc.fetch_credits()
            cred = (result or {}).get('credits') or {}
            label = ('已探测来源' if source else '默认端点') + ' cap=%d' % cap
            check(label + '：正常共享响应查询成功', bool(result and result.get('ok')), result)
            shared = cred.get('shared') or {}
            check(label + '：保留共享已用、剩余和上限',
                  (shared.get('used'), shared.get('remain'), shared.get('size')) == (used, remain, cap), shared)
            check(label + '：有效余额含共享，个人额度保持独立',
                  cred.get('remain_effective') == remain and cred.get('remain') == 0
                  and cred.get('used') == 0 and cred.get('size') == 0, cred)
            check(label + '：一次请求且记录来源和成功时间',
                  request.call_count == 1 and cred.get('source') == URL and cred.get('updated_at', 0) > 1)

    for source in (False, True):
        acc = account(credits=OLD)
        acc.last_quota_anomaly = {'reason': 'old error'}
        acc.credits_degraded = {'reason': 'old error'}
        with mock.patch.object(A, 'http_json', return_value=payload(remain=0, used=6000, exceeded=True)):
            result = acc._fetch_credits_via_source(None, SOURCE) if source else acc.fetch_credits()
        check('共享池耗尽是有效快照，不保留旧余额', bool(result and result.get('ok'))
              and (acc.credits or {}).get('remain_effective') == 0, result)
        check('成功刷新清理旧降级标记', acc.last_quota_anomaly is None and acc.credits_degraded is None)

    for available in (False, 'false', 0):
        acc = account()
        with mock.patch.object(A, 'http_json', return_value=payload(available=available)):
            result = acc._fetch_credits_via_source(None, SOURCE)
        check('不可用共享池不计入有效余额：%r' % available,
              bool(result and result.get('ok')) and acc.credits.get('remain_effective') == 0
              and acc.credits['shared'].get('available') is False, result)

    acc = account()
    with mock.patch.object(A, 'http_json', return_value=payload(cap=-1, remain=1000000)):
        result = acc._fetch_credits_via_source(None, SOURCE)
    check('不限量标志保留，占位余额不变成真实额度', bool(result and result.get('ok'))
          and A.shared_unlimited(acc.credits) and acc.credits.get('remain_effective') == 0, result)
    acc.reserve_credits = 9999
    check('不限量共享池不会被余额地板误停用', acc.reserve_blocked() is False)

    # 个人已耗尽但仍有合法共享池；不能再被 exceeded + 个人 0/0 的守卫拦截。
    q = payload(exceeded=True)
    q.update(userQuota={'total': 0, 'used': 0, 'remaining': 0}, addOnQuota={'total': 0, 'used': 0, 'remaining': 0})
    acc = account()
    with mock.patch.object(A, 'http_json', return_value=q):
        result = acc.fetch_credits()
    check('空个人额度并存时共享池仍可读取', result.get('ok') and acc.credits['remain_effective'] == 5746, result)

    for malformed in ({'message': 'no quota'}, {'orgResourcePackage': {}}, {'orgResourcePackage': {'used': 3}}):
        acc = account(credits=OLD)
        with mock.patch.object(A, 'http_json', return_value=malformed):
            result = acc.fetch_credits()
            via = acc._fetch_credits_via_source(None, SOURCE)
        check('缺少有效额度字段仍拒绝并保留旧快照', not result.get('ok') and via is None and acc.credits == OLD, result)

    degenerate = {'userQuota': {'total': 0, 'used': 0, 'remaining': 0}, 'isQuotaExceeded': True}
    acc = account(credits=OLD)
    with mock.patch.object(A, 'http_json', return_value=degenerate):
        result = acc.fetch_credits()
        via = acc._fetch_credits_via_source(None, SOURCE)
    check('个人全 0 退化体的保护仍生效', not result.get('ok') and via is None and acc.credits == OLD)

    acc = account(credits={'remain_effective': 'unknown'})
    with mock.patch.object(A, 'http_json', return_value=payload()):
        result = acc.fetch_credits()
    check('旧快照中的坏数值不影响本次成功刷新', result.get('ok') and acc.credits['remain_effective'] == 5746)

    acc = account(credits=OLD)
    with mock.patch.object(A, 'http_json', side_effect=OSError('offline')):
        result = acc.fetch_credits()
    check('网络失败仍保留旧快照和成功时间', not result.get('ok') and acc.credits == OLD)

    acc = account()
    pool = type('Pool', (), {'accounts': [acc]})()
    refresher = A.CreditsUsageRefresher(pool, interval_seconds=30)
    refresher.mark_used(acc.uid)
    with mock.patch.object(A, 'http_json', return_value=payload()):
        stats = refresher.tick_once()
    check('后台使用驱动刷新真正成功，不再持续退避', stats == {'refreshed': 1, 'failed': 0}
          and acc.credits_refresh_state == 'idle' and not acc.credits_pending, stats)

    account_dir = os.path.join(TMP.name, 'persisted')
    os.makedirs(account_dir)
    path = os.path.join(account_dir, 'quota-test.json')
    acc = account(path=path)
    A.save_credits_source(account_dir, 'intl', SOURCE['host'], SOURCE['path'], ['shared'])
    with mock.patch.object(A, 'http_json', return_value=payload()):
        result = acc.fetch_credits()
    saved = json.load(open(path)) if os.path.isfile(path) else {}
    restored = A.Account(saved)
    check('缓存来源路径写盘并可恢复共享余额', result.get('ok')
          and (restored.credits or {}).get('remain_effective') == 5746, saved.get('credits'))

check('出网次数为零', not network_attempts, network_attempts)
finish()
