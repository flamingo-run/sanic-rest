setup:
	@pip install -U pip poetry

dependencies:
	@make setup
	@poetry install --no-root --extras "auth"

update:
	@poetry update

test:
	@make check
	@make lint
	@make unit

check:
	@echo "Checking safety and integrity ..."
	poetry check
	poetry run safety check

lint:
	@echo "Checking code style ..."
	poetry run ruff check .
	poetry run ruff format --check .

style:
	@echo "Applying code style ..."
	poetry run ruff check . --fix
	poetry run ruff format .

unit:
	@echo "Running unit tests ..."

clean:
	@rm -rf .coverage coverage.xml dist/ build/ *.egg-info/

publish:
	@make clean
	@printf "\nPublishing lib"
	@make setup
	@poetry config pypi-token.pypi $(PYPI_API_TOKEN)
	@poetry publish --build
	@make clean

dev-install:
	@pip uninstall -y sanic_rest
	@\rm -rv dist/; poetry build --format sdist && tar -xvf dist/*.tar.gz -O '*/setup.py' > setup.py && python setup.py develop


.PHONY: setup dependencies update test check lint clean publish
