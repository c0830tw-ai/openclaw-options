#!/usr/bin/env python3
"""
將 web/index.html 同步到 options/index.html，並修正兩個版本的差異：
- API 路徑：port-8081 格式 → port-8080 格式
- vendor script 路徑：8081 docroot=web/ 用 vendor/...；
  8080 選單版網址是 /options/index.html，需改成 web/vendor/...
"""
import pathlib, sys

ROOT = pathlib.Path(__file__).parent
SRC  = ROOT / 'web' / 'index.html'
DST  = ROOT / 'index.html'

content = SRC.read_text(encoding='utf-8')
content = content.replace("'/api/data'",    "'/api/collar/data'")
content = content.replace("'/api/refresh'", "'/api/collar/refresh'")
content = content.replace('src="vendor/',   'src="web/vendor/')
DST.write_text(content, encoding='utf-8')
print(f'✓ synced web/index.html → index.html')
