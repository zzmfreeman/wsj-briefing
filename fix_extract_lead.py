#!/usr/bin/env python3
"""Fix extract_lead duplicate loop issue"""

path = '/Users/zzm/wsj-briefing/remote_collect.py'
with open(path, 'r') as f:
    content = f.read()

# Find the problematic section and replace it
old_section = '''    # 跳过面包屑导航（包含 ?mod=breadcrumb 或数字列表链接）
    for i, p in enumerate(paragraphs):
        # Skip markdown link paragraphs like [Author](url)
        if re.match(r'^\\[.+?\\]\\(https?://', p):
            continue
        # Skip paragraphs that are mostly markdown links
        plain = re.sub(r'\\[.+?\\]\\(.+?\\)', '', p).strip()
        if len(plain) < len(p) * 0.4:
            continue
    for i, p in enumerate(paragraphs):
        
        # Skip author bylines: [Name](https://.../author/...)
        if re.match(r'^\\[.+?\\]\\(https?://.+?/author/', p):
            continue
        
        # Skip paragraphs starting with markdown links: [Text](url)
        if re.match(r'^\\[.+?\\]\\(https?://', p):
            continue
        
        # Skip paragraphs with author links
        if '/author/' in p or 'news/author' in p:
            continue
        
        # Skip paragraphs that are mostly links (>70% link content)
        link_length = len(re.findall(r'\\[[^\\]]+\\]\\([^)]+\\)', p)) * 50
        if link_length > len(p) * 0.7:
            continue'''

new_section = '''    for i, p in enumerate(paragraphs):
        if len(p) < 50:
            continue
        
        # Skip author bylines: [Name](https://.../author/...) or [Name](url) when short
        if re.match(r'^\\[.+?\\]\\(https?://', p) and len(p) < 200:
            continue
        
        # Skip paragraphs that are mostly markdown links
        plain = re.sub(r'\\[.+?\\]\\(.+?\\)', '', p).strip()
        if len(plain) < len(p) * 0.4:
            continue
        
        # Skip paragraphs containing author/news links
        if '/author/' in p or 'news/author' in p:
            continue'''

if old_section in content:
    content = content.replace(old_section, new_section)
    with open(path, 'w') as f:
        f.write(content)
    print('✓ Fixed extract_lead - merged duplicate loops into single loop with all skip logic')
else:
    print('✗ Could not find the exact section to replace')
    print('\nSearching for partial matches...')
    if '跳过面包屑导航' in content:
        print('Found "跳过面包屑导航" marker')
    if 'for i, p in enumerate(paragraphs):' in content:
        count = content.count('for i, p in enumerate(paragraphs):')
        print(f'Found {count} occurrences of "for i, p in enumerate(paragraphs):"')
