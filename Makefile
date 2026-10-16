dist/:
	mkdir -p $@

build: dist/
	rm -rf src/microsre/__pycache__
	python3 build_zipapp.py

lint:
	ruff check --select I --fix src/microsre/*py tests/*py
	ruff format src/microsre/*py tests/*py
	ruff check src/microsre tests
	-mypy src/microsre
	wc -l src/microsre/*py | sort -n

test:
	PYTHONPATH=src python3 -m unittest discover -v -s tests -p "test_*.py"

docs:
	PYTHONPATH=src python3 generate_docs.py
