"""极简模板渲染器：只做 {{name}} 单次替换。

刻意不引入 Jinja2 之类的依赖，也不用 eval —— 模板里能做的只有占位符替换，
所有循环与条件都在 Python 侧展开。这样模板不会成为第二套业务逻辑，
夜间改界面时也不可能改坏算法。
"""

import re
from pathlib import Path

import config

_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class TemplateNotFound(Exception):
    pass


class Renderer:
    def __init__(self, template_dir=None):
        self.template_dir = Path(template_dir or config.TEMPLATE_DIR)
        self._cache = {}

    def source(self, name: str) -> str:
        if name not in self._cache:
            path = self.template_dir / name
            if not path.is_file():
                raise TemplateNotFound(str(path))
            self._cache[name] = path.read_text(encoding="utf-8")
        return self._cache[name]

    def render(self, name: str, **context) -> str:
        def replace(match):
            key = match.group(1)
            if key not in context:
                raise KeyError(f"模板 {name} 缺少占位符参数：{key}")
            value = context[key]
            return "" if value is None else str(value)

        return _PLACEHOLDER.sub(replace, self.source(name))


_default = None


def render(name: str, **context) -> str:
    global _default
    if _default is None:
        _default = Renderer()
    return _default.render(name, **context)
