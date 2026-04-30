"""
probe_shioaji.py
================
登入 Shioaji，印出：
  1. api.Contracts.Options 底下所有屬性名稱
  2. 加權指數的正確路徑（掃 TSE/OTC Indexs）
  3. 找到第一個 TXO 月份合約，印完整欄位
  4. 抓一個 TXO 合約的 snapshot，確認 bid/ask 欄位名稱

用法:
    python3 probe_shioaji.py
"""

import os
import sys

# 嘗試載入 .env（若有裝 python-dotenv）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import shioaji as sj

API_KEY = os.environ.get('SHIOAJI_API_KEY', '')
SECRET_KEY = os.environ.get('SHIOAJI_SECRET_KEY', '')

if not API_KEY or not SECRET_KEY:
    print('[ERROR] SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY 未設定')
    print('  請先設定環境變數或在 .env 填入金鑰')
    sys.exit(1)


def section(title: str):
    print()
    print('=' * 60)
    print(f'  {title}')
    print('=' * 60)


def probe_options(api):
    section('1. api.Contracts.Options 可用屬性')
    opts = api.Contracts.Options
    attrs = [a for a in dir(opts) if not a.startswith('_')]
    print(f'共 {len(attrs)} 個屬性:')
    for a in sorted(attrs):
        print(f'  {a}')


def probe_index(api):
    section('2. 加權指數路徑探查')

    # 試 Indexs（原始程式用的拼法）
    for top_attr in ('Indexs', 'Indexes', 'Index'):
        top = getattr(api.Contracts, top_attr, None)
        if top is None:
            print(f'  api.Contracts.{top_attr} → 不存在')
            continue
        print(f'  api.Contracts.{top_attr} → 存在')
        # 列出子屬性
        for exchange in dir(top):
            if exchange.startswith('_'):
                continue
            ex_obj = getattr(top, exchange)
            codes = [c for c in dir(ex_obj) if not c.startswith('_')]
            print(f'    .{exchange}: {codes[:10]}{"..." if len(codes) > 10 else ""}')
            # 試找 001
            if '001' in codes:
                c = getattr(ex_obj, '001', None)
                print(f'    → 找到 001: {c}')


def probe_txo_contract(api):
    section('3. TXO 合約完整欄位（第一個可用月份）')
    opts = api.Contracts.Options
    txo_attrs = sorted([a for a in dir(opts) if a.startswith('TXO')])
    if not txo_attrs:
        print('  找不到任何 TXO 開頭的屬性')
        return None, None

    print(f'TXO 月份列表: {txo_attrs}')
    first_attr = txo_attrs[0]
    chain = list(getattr(opts, first_attr))
    print(f'\n使用 {first_attr}，共 {len(chain)} 個合約')

    if not chain:
        print('  chain 為空')
        return None, None

    c = chain[0]
    print(f'\n第一個合約物件型態: {type(c)}')
    print('欄位:')
    for field in dir(c):
        if field.startswith('_'):
            continue
        try:
            val = getattr(c, field)
            if callable(val):
                continue
            print(f'  {field}: {val!r}')
        except Exception as e:
            print(f'  {field}: <error: {e}>')

    return first_attr, chain


def probe_snapshot(api, chain):
    section('4. Snapshot bid/ask 結構')
    if not chain:
        print('  無合約可用')
        return

    # 取一個 Put 和一個 Call
    from shioaji.constant import OptionRight
    puts = [c for c in chain if c.option_right == OptionRight.Put]
    calls = [c for c in chain if c.option_right == OptionRight.Call]

    targets = []
    if puts:
        targets.append(('Put', puts[0]))
    if calls:
        targets.append(('Call', calls[0]))

    for label, contract in targets:
        print(f'\n--- {label}: strike={contract.strike_price} ---')
        try:
            snaps = api.snapshots([contract])
            snap = snaps[0]
            print(f'  型態: {type(snap)}')
            for field in dir(snap):
                if field.startswith('_'):
                    continue
                try:
                    val = getattr(snap, field)
                    if callable(val):
                        continue
                    print(f'  {field}: {val!r}')
                except Exception as e:
                    print(f'  {field}: <error: {e}>')
        except Exception as e:
            print(f'  snapshot 失敗: {e}')


def main():
    print('Shioaji Probe — 登入中...')
    api = sj.Shioaji(simulation=False)
    api.login(API_KEY, SECRET_KEY, contracts_timeout=30000)
    print('登入成功')

    try:
        probe_options(api)
        probe_index(api)
        first_attr, chain = probe_txo_contract(api)
        probe_snapshot(api, chain)
    finally:
        api.logout()
        print('\n登出完成')


if __name__ == '__main__':
    main()
