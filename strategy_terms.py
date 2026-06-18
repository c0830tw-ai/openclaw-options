"""strategy_terms.py — 英文策略代號 / 術語 → 中文白話備註

給 Telegram 自動報告（morning_report / evening_report / telegram_bot）共用，
讓訊息裡的英文（hui-full、cal-std、put_only、theta…）後面自動補中文。

用法：
    from strategy_terms import gloss, annotate
    gloss('hui-full')                 -> 'hui-full（輝哥全套：買1put+賣2put+賣1call）'
    annotate('cal-std +5~+8% theta 穩') -> 'cal-std（標準日曆…）+5~+8% theta（時間價值收益）穩'

代號定義同 backtest_all.py（BP=買put、SP=賣put、SC=賣call）。
"""
import re

# key 一律小寫；底線與連字號版本都列，方便不同來源
_TERMS = {
    'naked':        '裸部位：純多頭無避險',
    'put-only':     '純買 put 保護',
    'put_only':     '純買 put 保護',
    'collar':       '領口：買 put + 賣 call',
    'hui-ratio':    '輝哥比例式：買1put + 賣2put',
    'hui_ratio':    '輝哥比例式：買1put + 賣2put',
    'hui-full':     '輝哥全套：買1put + 賣2put + 賣1call',
    'hui_full':     '輝哥全套：買1put + 賣2put + 賣1call',
    'cal-std':      '標準日曆價差：買遠月 + 賣近月',
    'cal_std':      '標準日曆價差：買遠月 + 賣近月',
    'cal-reverse':  '反向日曆：買近月 + 賣遠月',
    'cal-rev':      '反向日曆：買近月 + 賣遠月',
    'ic':           '鐵兀鷹：4 腳區間收租',
    'fallback':     '後備方案',
    'theta':        '時間價值收益',
    'vega':         '波動率敏感度',
}


def _zh(term):
    if term is None:
        return None
    return _TERMS.get(str(term).strip().lower())


def gloss(term):
    """單一策略代號 → 'term（中文）'；查無對應則原樣回傳。"""
    z = _zh(term)
    return f'{term}（{z}）' if z else term


# 自由文字裡嵌入的代號補中文；同一個詞整段只標一次，避免重複囉嗦
_PAT = re.compile(
    r'(?<![\w-])(' +
    '|'.join(re.escape(k) for k in sorted(_TERMS, key=len, reverse=True)) +
    r')(?![\w-])',
    re.IGNORECASE,
)


def annotate(text):
    """整段文字內所有已知英文代號各補一次中文。"""
    if not text:
        return text
    seen = set()

    def _repl(m):
        word = m.group(1)
        key = word.lower()
        if key in seen:
            return word
        seen.add(key)
        z = _TERMS.get(key)
        return f'{word}（{z}）' if z else word

    return _PAT.sub(_repl, text)
