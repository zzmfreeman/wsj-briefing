#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
from remote_collect import extract_lead

# Test English article
test_en = "Updated Jan. 28, 2025 12:44 pm ETDeepSeek has Silicon Valley in awe and investors in a frenzy. The Chinese artificial-intelligence upstart has shot to prominence."
lead, body = extract_lead(test_en)
print(f"EN lead: {lead[:80]}...")
print(f"EN body: {body[:80] if body else '(empty)'}")

# Test Chinese article
test_cn = " 2026年7月17日 16:52 CST中国领导人习近平支持构建开源人工智能模型，对这一助力中国在全球影响力赛道上追赶美国的策略予以肯定。"
lead, body = extract_lead(test_cn)
print(f"\nCN lead: {lead[:80]}...")
print(f"CN body: {body[:80] if body else '(empty)'}")
