"""留出集：调参时完全没看过，用来检验真实泛化。

标注规则：主期望一个，另给可接受的备选（top2 命中即算对）。
这些句子刻意换了说法，避免和 calibrate.py 撞词。
"""
HOLDOUT = [
    ("帮我把这个表格转成 csv", "answer", {"answer"}),
    ("我在想是不是该辞职了", "care", {"care", "probe"}),
    ("你饿不饿", "share", {"share", "care"}),
    ("笑死", "dodge", {"dodge", "tease"}),
    ("鱼鱼", "dodge", {"dodge", "answer"}),
    ("你好笨啊哈哈哈", "tease", {"tease"}),
    ("我今天升职了！", "care", {"care", "answer"}),      # 该跟着高兴
    ("帮我看看这个报错", "answer", {"answer"}),
    ("你还喜欢看球吗", "share", {"share"}),
    ("过来哄哄我", "coy", {"coy", "care"}),
    ("你昨天怎么没回我", "probe", {"probe", "tease"}),
    ("算了不说了", "dodge", {"dodge", "probe"}),
    ("你觉得我该去吗", "probe", {"probe", "care"}),
    ("我想你了", "care", {"care", "coy"}),
    ("在干嘛", "share", {"share", "dodge", "answer"}),
]
