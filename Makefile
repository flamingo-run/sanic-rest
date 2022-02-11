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
	@poetry check

lint:
	@echo "Checking code style ..."
	@poetry run pylint --rcfile=./.pylintrc sanic_rest

unit:
	@echo "Running unit tests ..."
	@poetry run nosetests

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
