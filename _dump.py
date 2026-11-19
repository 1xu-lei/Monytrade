"""构建期：导出渲染结果供人工核对（交付前删除）。"""
import re
import sys
from pathlib import Path

from core import models, reconciler
from web import svg, views

conn = models.connect_ready()
for order_id in ("MT20260921-001", "MT20260919-007", "MT20260920-001", "MT20260919-003"):
    trail = reconciler.build_trail(conn, order_id)
    markup = svg.build(trail, title=order_id)
    Path(f"_svg_{order_id}.svg").write_text(markup, encoding="utf-8")
    print("=" * 70)
    print(order_id, "broken=", trail["broken_index"], trail["gap_text"])
    for stage in trail["stages"]:
        print(f"  {stage['name']:<8} state={stage['state']:<7} t={stage['time'] or '-':<20} "
              f"amt={stage['amount_fen']}")
    xs = re.findall(r'class="node[^"]*" cx="([\d.]+)"', markup)
    print("  node x:", xs)
    print("  labels:", len(re.findall(r"<text", markup)))

for name in ("overview", "anomalies", "trail", "assistant", "import"):
    pass
Path("_page_overview.html").write_text(views.overview(conn), encoding="utf-8")
Path("_page_anomalies.html").write_text(views.anomalies_page(conn, {
    "label": "", "severity": "", "status": "", "date_from": "", "date_to": ""}), encoding="utf-8")
Path("_page_trail.html").write_text(views.trail_page(conn, "MT20260921-001"), encoding="utf-8")
Path("_page_assistant.html").write_text(
    views.assistant_page(conn, "这个月手续费多收了多少"), encoding="utf-8")
Path("_page_import.html").write_text(views.import_page(conn), encoding="utf-8")
print("pages written")
