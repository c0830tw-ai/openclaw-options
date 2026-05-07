#!/usr/bin/env python3
"""
將 web/index.html 同步到 options/index.html，
並將 API 路徑從 port-8081 格式改成 port-8080 格式。
"""
import pathlib, sys

ROOT = pathlib.Path(__file__).parent
SRC  = ROOT / 'web' / 'index.html'
DST  = ROOT / 'index.html'

content = SRC.read_text(encoding='utf-8')
content = content.replace("'/api/data'",    "'/api/collar/data'")
content = content.replace("'/api/refresh'", "'/api/collar/refresh'")
DST.write_text(content, encoding='utf-8')
print(f'✓ synced web/index.html → index.html')
