"""她动手时的参数格式 —— 单一真相源。

分工（别再混起来）：
- toolguide.py：什么时候该动手、做不到怎么说。**每轮常驻**，因为它教的是判断。
- 这里：真要动手那一下的参数形状。**按需注入**，因为只有那一轮用得上。

写这个文件踩过的坑：
1. 只写 `feiyu(action, target, text, question)` 这四个参数，别编别的名字。
   1.3.5 曾给「发说说」写过 `visibility=`，而工具签名里根本没有它 ——
   管理员照着发，AstrBot 直接 TypeError，是「规范教你调一个不存在的参数」的教科书案例。
2. 不写真实 BV / 真实 URL —— 她会把示例值当内容照发（发评论、发说说都不可逆）。
3. tier="admin" 的只在管理员会话里出现；普通用户连「她会这个」都不该被告知。
4. 缺哪个动词就不渲染哪一行，别教她一条走不通的路（插件被禁用时尤其明显）。
5. 一个动词一行，别写成段落：命中的动词才进 prompt，最多两个。

数据形状刻意做成结构（verb / form / note / tier / signals），这样
「渲染给模型看的文本」和「启动自检用的参数名」共用同一份常量，不会漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field

NL = chr(10)

# 一次注入最多带几个动词的格式：多一个 = 多一次注意力摊薄
MAX_VERBS = 2

# 同时命中时谁先（数值大者优先）。
# 「看视频」在等交付物，「问妈妈」是她卡住的兜底，两者同时出现先看再问。
PRIORITY = {
    "看视频": 90,
    "问妈妈": 85,
    "发评论": 80,
    "读评论": 55,
    "搜": 70,
    "画": 60,
    "说": 58,
    "读网页": 52,
    "截图": 50,
    "认人": 45,
    "发说说": 30,
    "改昵称": 28,
    "改签名": 28,
    "改性别": 28,
}


@dataclass(frozen=True)
class Spec:
    """一个动词的「怎么填」。"""

    verb: str
    form: str                      # 照抄的调用形状（尖括号是占位，不是内容）
    note: str = ""                 # 只有最易错的那几个才写，别每条都写
    tier: str = "all"              # all / admin
    # 严格信号：出现就注入，几乎不误报
    strict: tuple[str, ...] = field(default_factory=tuple)
    # 弱信号：只在「上文有目标」时才注入（链接、媒体、她上一轮说过要去做）
    weak: tuple[str, ...] = field(default_factory=tuple)


def _s(verb, form, note="", tier="all", strict=(), weak=()):
    return Spec(verb=verb, form=form, note=note, tier=tier, strict=tuple(strict), weak=tuple(weak))


SPECS: dict[str, Spec] = {
    s.verb: s
    for s in [
        _s(
            "看视频",
            'feiyu(action="看视频", target="<视频链接或本地路径>", question="<想特别看清什么，可不填>")',
            note="他发来链接/视频，或问「这讲了啥」时用；question 只在想看清某处时填",
            strict=(
                r"https?://[^\s]*(?:bilibili\.com|b23\.tv|douyin\.com|youtu\.be|youtube\.com"
                r"|kuaishou\.com|xiaohongshu\.com|weibo\.com|ixigua\.com)",
                r"BV[0-9A-Za-z]{10}",
                r"av\d{6,}",
            ),
            weak=(r"看看", r"讲了", r"画面", r"里面是", r"这视频", r"这段", r"这期", r"怎么样"),
        ),
        _s(
            "搜",
            'feiyu(action="搜", target="<要搜的那句话>")',
            note="target 里写要搜的那句话，不是网址",
            strict=(
                r"搜(?:一|个|下|搜)?\s*[^\s，。！？]",
                r"查(?:一|个|下)下|查查|帮我查|查一下",
                r"是谁|什么梗|多少钱|哪年|哪一年|什么来头|怎么回事|什么情况|有没有人知道",
            ),
        ),
        _s(
            "读网页",
            'feiyu(action="读网页", target="<网页链接>")',
            strict=(
                r"读一下|读读|正文|这篇文章|网页内容|打开这个链接|(?:这|那)页(?:写了|写的|内容|正文)",
            ),
            weak=(r"https?://",),
        ),
        _s(
            "截图",
            'feiyu(action="截图", target="<网页链接>")',
            strict=(r"截(?:个|一|下)?图|截图|截一下|长什么样|长啥样",),
        ),
        _s(
            "说",
            'feiyu(action="说", text="<要念出来的那段话>")',
            strict=(
                r"语音|念一下|念给我|念一段|用(?:你的|你)?声音|说给我听|读给我听|发条语音",
            ),
        ),
        _s(
            "画",
            'feiyu(action="画", text="<画面描述>")',
            strict=(r"画(?:一|个|张|只|下|幅|两)|来张图|来一幅|画张|画个|出张图",),
        ),
        _s(
            "认人",
            'feiyu(action="认人")',
            note="只对刚看过的那段画面有效",
            strict=(r"认一下|认认|这是谁|那个人是谁",),
            weak=(r"是谁",),
        ),
        _s(
            "问妈妈",
            'feiyu(action="问妈妈", question="<你拿不准的事>", text="<当时在做什么>")',
            note="问完答案当场回到你手里，别干等也别自己编一段",
            strict=(
                r"问.{0,4}(?:妈|妈妈)",
                r"问问她|去问她|问一下她|请教她|找你妈",
            ),
            weak=(r"你自己(?:想|来|看着办)|实在不行|搞不定",),
        ),
        _s(
            "发评论",
            'feiyu(action="发评论", target="<B站视频链接>", text="<评论原文>")',
            note="写完直接发，发完把原文和链接告诉主人",
            tier="admin",
            strict=(r"评论|回他|评一句|留个言",),
        ),
        _s(
            "读评论",
            'feiyu(action="读评论", target="<B站视频链接>")',
            note="只读不发言，按热度取前几条，用自己的话说别照念",
            strict=(
                r"读.{0,8}评论|看看?评论|评论区(?:都|里|底下)|评论(?:区)?(?:里|底下)(?:都|在)?(?:说|聊|讲|写)",
                r"大家(?:都)?在(?:说|聊|评)|评论都(?:说|写)|底下(?:都|在).{0,4}(?:说|聊)",
            ),
            weak=(r"评论区|评论里|评论都",),
        ),
        _s(
            "发说说",
            'feiyu(action="发说说", text="<正文；想限定可见范围就在正文里写清楚「仅自己可见」>")',
            tier="admin",
            strict=(r"发(?:条|个|一)?说说|说说(?:发|写)|空间.{0,4}(?:发|动态)",),
        ),
        _s("改昵称", 'feiyu(action="改昵称", text="<新昵称>")', tier="admin",
           strict=(r"昵称|改个名|换个名|改名",)),
        _s("改签名", 'feiyu(action="改签名", text="<新签名>")', tier="admin",
           strict=(r"签名",)),
        _s("改性别", 'feiyu(action="改性别", text="<男|女>")', tier="admin",
           strict=(r"性别",)),
    ]
}

def head() -> str:
    return "这次真要动手，就照下面这一行填（尖括号换成真的，别把 action= 这些念给主人听）："


def render_for(verbs, *, available=None, with_head: bool = True) -> str:
    """只渲染这几个动词的调用形状。没注册上的自动跳过；一个都没命中就返回空串。"""
    have = None if available is None else set(available)
    lines = []
    for verb in verbs or ():
        spec = SPECS.get(verb)
        if spec is None:
            continue
        if have is not None and verb not in have:
            continue
        lines.append(spec.form)
        if spec.note:
            lines.append("  · " + spec.note)
    if not lines:
        return ""
    body = NL.join(lines)
    return (head() + NL + body) if with_head else body


def render_all(admin: bool = False, available=None) -> str:
    """全量渲染（兼容旧 apispec.render 的行为；正常路径应该用 render_for）。"""
    have = None if available is None else set(available)
    verbs = [
        v for v, s in SPECS.items()
        if (have is None or v in have) and (admin or s.tier != "admin")
    ]
    verbs.sort(key=lambda v: -PRIORITY.get(v, 0))
    return render_for(verbs, available=available)


def admin_verbs(available=None) -> list[str]:
    have = None if available is None else set(available)
    return [
        v for v, s in SPECS.items()
        if s.tier == "admin" and (have is None or v in have)
    ]


def form_params(form: str) -> set[str]:
    """从 form 里抠出用到的参数名 —— 供启动自检用（`visibility=` 那类错就是这样抓到的）。"""
    import re

    return set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", form or ""))
