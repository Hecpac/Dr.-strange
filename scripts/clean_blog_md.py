"""Clean the extracted blog post markdown by stripping NotebookLM UI cruft."""
import re, os, time

src = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm/blog_beyond_v3_1779478134.md"
out = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm/blog_beyond_clean.md"

raw = open(src).read()

# Find the blog post body (starts at "Beyond the Prompt:" title that appears after "content_copy Basado en 52 fuentes")
m = re.search(r'Basado en \d+ fuentes\s*Beyond the Prompt', raw)
if m:
    body = raw[m.end()-len('Beyond the Prompt'):]
else:
    body = raw[raw.find('Beyond the Prompt'):]

# Cut at the conclusion footer
end_markers = ['thumb_upInforme positivo', 'Es posible que NotebookLM muestre información imprecisa']
for marker in end_markers:
    idx = body.find(marker)
    if idx > 0:
        body = body[:idx]
        break

# Normalize spacing around section dividers
body = body.replace('-' * 80, '\n\n---\n\n')

# Add markdown structure
sections = [
    ('Introduction: The Agentic Mirage', '## Introduction: The Agentic Mirage\n\n'),
    ('1. The "15x" Token Tax', '\n\n## 1. The "15x" Token Tax and Operational Margin Erosion\n\n'),
    ('2. Stop Augmenting, Start Internalizing', '\n\n## 2. Stop Augmenting, Start Internalizing (The SKILL0 Breakthrough)\n\n'),
    ('3. Instruction Layering:', '\n\n## 3. Instruction Layering: The Hierarchy of Durable Execution\n\n'),
    ('4. The Forgetting Paradox:', '\n\n## 4. The Forgetting Paradox: Architecture Over Memory\n\n'),
    ('5. Bridging the Reliability Gap:', '\n\n## 5. Bridging the Reliability Gap: Tool Selection and Staggered Deployment\n\n'),
    ('Conclusion: The Path to Autonomous Intelligence', '\n\n## Conclusion: The Path to Autonomous Intelligence\n\n'),
]

# Insert markdown headers
for plain, replacement in sections:
    body = body.replace(plain, replacement, 1)

# Ensure title at top
body = body.lstrip()
if body.startswith('Beyond the Prompt'):
    title_end = body.find('Introduction')
    if title_end > 0:
        body = '# Beyond the Prompt: 5 Surprising Realities of Building Professional Multi-Agent Systems\n\n' + body[title_end:]

# Normalize whitespace
body = re.sub(r'\n{4,}', '\n\n\n', body)
body = re.sub(r' {2,}', ' ', body)

# Save
with open(out, 'w') as f:
    f.write(body)

print(f"saved {len(body)} chars -> {out}")
print("\n=== PREVIEW ===\n")
print(body[:6000])
