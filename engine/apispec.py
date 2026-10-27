"""兼容层：调用格式的真身在 engine/specs.py。

这个文件以前是格式的唯一起源地，1.3.6 起职责搬走了，原因有两条：
1. 13 个动词全量渲染太贵（管理员会话 761 字，每轮都发），而这一轮最多用得上一个；
   现在由 selector.py 按信号挑，只渲染命中的那一两个。
2. 格式和「什么时候用」拆成两份文件后开始漂移 —— 1.3.5 在这里给「发说说」写了
   `visibility=`，而 feiyu 的签名里只有 action/target/text/question。管理员照着发必 TypeError。
   所以格式统一收进 specs.py，自检能直接比对参数名。

保留 render() 只是为了不让外部调用方（工具脚本、测试）因为搬家而报错。
新代码请直接用 specs.render_for(verbs) / specs.render_all()。
"""

from __future__ import annotations

from . import specs


def render(admin: bool = False, available=None) -> str:
    """全量渲染（旧行为）。新代码别用这个 —— 它会把 13 个动词的格式都塞进去。"""
    return specs.render_all(admin=admin, available=available)


def prepare(available=None) -> list[str]:
    """旧接口：拿到此刻能渲染的动词清单。"""
    have = None if available is None else set(available)
    return [v for v in specs.SPECS if have is None or v in have]
