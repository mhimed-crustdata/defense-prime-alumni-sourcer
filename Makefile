.PHONY: setup run demo test
setup:
	@test -f .env || cp .env.example .env; echo "Edit .env and set CRUSTDATA_API_KEY"
run:
	python3 sourcer.py
demo:
	python3 sourcer.py --dry-run --all
test:
	python3 -m unittest discover -s tests -v
