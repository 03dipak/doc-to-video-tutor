PY := .venv/bin/python
STUDIO := .venv/bin/doc-to-studio
SRC ?= modules/08_concepts_mod03_gates.md
OUT ?= output/mod03_docsas_code
MIN ?= 4.0
VOICE ?= mhe-mix

.PHONY: plan review verify build bakeoff test lint typecheck clean

## Docs-as-Code: regenerate the whole lesson from the source Markdown.
plan:
	$(STUDIO) build $(SRC) --minutes $(MIN) --narr-voice $(VOICE) --out $(OUT) --skip-video

## Deterministic QA on the last saved plan (no LLM, no render).
verify:
	$(STUDIO) verify $(OUT).plan.json

## Full render from the saved plan (deterministic, no LLM).
build:
	$(STUDIO) render $(OUT).plan.json --out $(OUT)

bakeoff:
	@echo "E1.3 voice bake-off (recorded in LLD §12/§14): 4 voices x scenes 2/4/6"
	@echo "en-IN-NeerjaNeural / en-IN-PrabhatNeural / hi-IN-SwaraNeural / hi-IN-MadhurNeural @ -8%/+0Hz/+0%"

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check src tests

typecheck:
	.venv/bin/mypy src

clean:
	rm -rf output/voice_bakeoff output/*.rejected.* 2>/dev/null || true