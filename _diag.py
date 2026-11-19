"""临时诊断脚本（构建期使用，交付前删除）。"""
import json

from core import models, reconciler

conn = models.connect_ready()
stats = reconciler.stats(conn)
print(json.dumps(stats, ensure_ascii=False, indent=2))
print("--- anomalies ---")
for row in models.list_anomalies(conn):
    print(f"{row['anomaly_id']:<28} {row['label_code']:<16} {row['severity']:<6} "
          f"diff={row['diff_fen']:>7} gap={row['gap_fen']:>7} status={row['status']}")
trail = reconciler.build_trail(conn, "MT20260921-001")
print("--- trail MT20260921-001 ---")
print("broken_index", trail["broken_index"], trail["gap_text"], trail["reason"])
for stage in trail["stages"]:
    print(" ", stage["name"], stage["state"], stage["time"], stage["amount_fen"], stage["ref"], stage["note"])
