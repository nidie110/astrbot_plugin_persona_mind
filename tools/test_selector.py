"""选择器离线自测：拿真实语料里出现过的说法过一遍，看选出来的动词对不对。

跑法： python tools/test_selector.py
不打日志、不联网，纯本地。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import selector, specs  # noqa: E402

ALL = set(specs.SPECS)

CASES = [
    # (消息, 是否管理员, 有没有目标, 期望选中的动词)
    ("https://www.bilibili.com/video/BV1jhn16GEcv 这期讲了啥", True, True, ["看视频"]),
    ("BV1RGNJzCEXq 你看看", True, True, ["看视频"]),
    ("你搜搜看，一首关于鲸鱼的歌", True, False, ["搜"]),
    ("你可以查查看他现在什么情况", True, False, ["搜"]),
    ("这东西是谁做的啊", True, False, ["搜"]),
    ("帮我画一只猫", True, False, ["画"]),
    ("画个横版的水墨山", True, False, ["画"]),
    ("用语音说我爱你", True, False, ["说"]),
    ("念一下你昨天写的那段", True, False, ["说"]),
    ("你能在这条底下评论一句吗", True, True, ["发评论"]),
    ("你去 BV1tjnh6sEzW 底下回他一句", True, True, ["看视频", "发评论"]),
    ("你问问她，这个怎么修", True, False, ["问妈妈"]),
    ("实在不行去问她", True, False, ["问妈妈"]),
    ("问问你妈怎么办", True, False, ["问妈妈"]),
    ("帮我截个图看看这页长什么样", True, False, ["截图"]),
    ("改个昵称叫小鲸鱼", True, False, ["改昵称"]),
    ("发条说说今天好累", True, False, ["发说说"]),
    ("https://example.com/article/123 你读一下", True, True, ["读网页"]),
    # 负例：不该注入
    ("那你说说为什么", True, False, []),
    ("陪我说说话", True, False, []),
    ("评论区为什么打不开", True, False, []),
    ("没在忙，不用管我", True, False, []),
    ("/help 看看命令", True, False, []),
    ("feiyu(action=\"问妈妈\", question=\"x\")", True, False, []),
    ("嗯嗯我在听", True, False, []),
    ("今天好累啊", True, False, []),
    # 非管理员：admin 动词一律不出现
    ("你能在这条底下评论一句吗", False, True, []),
    ("发条说说", False, False, []),
    # 弱信号：没目标不认，有目标才认
    ("看看这个怎么样", True, False, []),
    ("看看这个怎么样", True, True, ["看视频"]),
]


def main() -> int:
    bad = 0
    for text, admin, target, want in CASES:
        got = selector.pick(text, admin=admin, available=ALL, has_target=target)
        if got != want:
            bad += 1
            print(f"FAIL  {text[:36]:<38} admin={admin} target={target}")
            print(f"      期望 {want}  实得 {got}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
