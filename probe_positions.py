"""一次性 probe — 看 Shioaji list_positions 對你帳戶實際回傳什麼。"""
import os, sys
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
import shioaji as sj

api = sj.Shioaji(simulation=False)
api.login(os.environ['SHIOAJI_API_KEY'], os.environ['SHIOAJI_SECRET_KEY'])
print('logged in')
print()

# Account refs
print(f'stock_account:  {api.stock_account}')
print(f'futopt_account: {api.futopt_account}')
print()

# 期權持倉
print('═══ list_positions(futopt_account) ═══')
try:
    fp = api.list_positions(api.futopt_account)
    print(f'共 {len(fp)} 筆')
    for i, p in enumerate(fp):
        print(f'\n[{i+1}] type: {type(p).__name__}')
        for attr in dir(p):
            if attr.startswith('_'):
                continue
            try:
                val = getattr(p, attr)
                if callable(val):
                    continue
                print(f'    {attr}: {val!r}')
            except Exception as e:
                print(f'    {attr}: <err {e}>')
except Exception as e:
    print(f'失敗: {e}')

print()
print('═══ list_positions(stock_account) ═══')
try:
    sp = api.list_positions(api.stock_account)
    print(f'共 {len(sp)} 筆')
    for i, p in enumerate(sp[:3]):  # 只印前 3 筆
        print(f'\n[{i+1}] type: {type(p).__name__}')
        for attr in dir(p):
            if attr.startswith('_'):
                continue
            try:
                val = getattr(p, attr)
                if callable(val):
                    continue
                print(f'    {attr}: {val!r}')
            except Exception:
                pass
except Exception as e:
    print(f'失敗: {e}')

api.logout()
