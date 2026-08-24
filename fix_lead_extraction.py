#!/usr/bin/env python3
"""Fix extract_lead to skip author bylines and links"""
import re

filepath = '/Users/zzm/wsj-briefing/remote_collect.py'

with open(filepath, 'r') as f:
    content = f.read()

# Find and replace the extract_lead function
old_function = '''def extract_lead(cleaned_text):
    """从清洗后的文本提取导语（第一段有意义的段落）和摘要正文。
    跳过时间戳、作者、版权信息等元数据。
    """
    if not cleaned_text:
        return "", ""
    
    # 清洗时间戳前缀
    tz_list = r'(?:ET|EST|EDT|CST|CDT|PST|PDT|MST|MDT|GMT|UTC|BST|CET|IST|HKT|JST|KST|SGT|AEST|ACST)'
    cleaned = re.sub(r'^\\s*Updated \\w+\\.? \\d{1,2},? \\d{4} \\d{1,2}:\\d{2} [ap]m ' + tz_list, '', cleaned_text)
    cleaned = re.sub(r'^\\s*\\d{4}年\\d{1,2}月\\d{1,2}日\\s+\\d{1,2}:\\d{2}\\s+' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\\s*\\w+\\.? \\d{1,2},? \\d{4} \\d{1,2}:\\d{2} [ap]m ' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\\s*\\d{1,2}:\\d{2} [ap]m ' + tz_list, '', cleaned)
    
    # 按双换行或段落分割
    paragraphs = re.split(r'\\n\\n+', cleaned)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    
    lead = ""
    body = ""
    found_lead = False
    
    for i, p in enumerate(paragraphs):
        if len(p) < 30:
            continue
        
        if not found_lead:
            lead = p[:500]
            found_lead = True
            if i + 1 < len(paragraphs):
                body = "\\n\\n".join(paragraphs[i+1:])
            break
    
    return lead, body'''

new_function = '''def extract_lead(cleaned_text):
    """从清洗后的文本提取导语（第一段有意义的段落）和摘要正文。
    跳过时间戳、作者、版权信息等元数据。
    """
    if not cleaned_text:
        return "", ""
    
    # 清洗时间戳前缀
    tz_list = r'(?:ET|EST|EDT|CST|CDT|PST|PDT|MST|MDT|GMT|UTC|BST|CET|IST|HKT|JST|KST|SGT|AEST|ACST)'
    cleaned = re.sub(r'^\\s*Updated \\w+\\.? \\d{1,2},? \\d{4} \\d{1,2}:\\d{2} [ap]m ' + tz_list, '', cleaned_text)
    cleaned = re.sub(r'^\\s*\\d{4}年\\d{1,2}月\\d{1,2}日\\s+\\d{1,2}:\\d{2}\\s+' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\\s*\\w+\\.? \\d{1,2},? \\d{4} \\d{1,2}:\\d{2} [ap]m ' + tz_list, '', cleaned)
    cleaned = re.sub(r'^\\s*\\d{1,2}:\\d{2} [ap]m ' + tz_list, '', cleaned)
    
    # 按双换行或段落分割
    paragraphs = re.split(r'\\n\\n+', cleaned)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]
    
    lead = ""
    body = ""
    found_lead = False
    
    for i, p in enumerate(paragraphs):
        # Skip very short paragraphs
        if len(p) < 50:
            continue
        
        # Skip author bylines: [Name](https://.../author/...) or [Name](http://.../author/...)
        if re.search(r'\\[.+?\\]\\(https?://[^)]+/author/', p):
            continue
        
        # Skip paragraphs that are mostly markdown links (>70% link content)
        link_chars = len(re.findall(r'\\[[^\\]]+\\]\\([^)]+\\)', p)) * 50  # Approximate link length
        if link_chars > len(p) * 0.7:
            continue
        
        # Skip paragraphs with multiple author names pattern
        if re.search(r'By\\s+\\[.+?\\]\\(https?://', p):
            continue
        
        if not found_lead:
            lead = p[:500]
            found_lead = True
            if i + 1 < len(paragraphs):
                body = "\\n\\n".join(paragraphs[i+1:])
            break
    
    return lead, body'''

if old_function in content:
    content = content.replace(old_function, new_function)
    with open(filepath, 'w') as f:
        f.write(content)
    print('✓ Fixed extract_lead function to skip author bylines')
else:
    print('✗ Could not find the exact function to replace')
    print('Searching for extract_lead...')
    if 'def extract_lead' in content:
        print('Found extract_lead, but exact match failed')
    else:
        print('extract_lead not found in file')
