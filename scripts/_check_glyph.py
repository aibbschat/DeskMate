import zipfile, re, os
p = os.path.expanduser('~/Documents/已出版/20.国子监秘闻录/国子监秘闻录.docx')
xml = zipfile.ZipFile(p).read('word/document.xml').decode('utf-8')
ts = [t for t in re.findall(r'<w:t[^>]*>([^<]*)</w:t>', xml) if t.strip()]
print('non-empty w:t nodes:', len(ts))
def cps(s):
    return ''.join('U+%04X ' % ord(c) if ord(c) > 0x2000 else c for c in s[:40])
print('--- first 10 stored paragraphs ---')
for t in ts[:10]:
    print(cps(t))