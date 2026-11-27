from pathlib import Path
import json
def register(ctx):
    sections=json.loads((Path(__file__).parent/"operating.json").read_text())
    for i,section in enumerate(sections):
        ctx.register_system_prompt_section(f"zeus-guidance.operating-{i+1}",section,max_chars=4000)
