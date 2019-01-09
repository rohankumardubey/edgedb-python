.PHONY: compile debug test quicktest clean all


PYTHON ?= python
ROOT = $(dir $(realpath $(firstword $(MAKEFILE_LIST))))


all: compile


clean:
	rm -fr dist/ doc/_build/
	rm -fr edgedb/pgproto/*.c edgedb/pgproto/*.html
	rm -fr edgedb/pgproto/codecs/*.html
	rm -fr edgedb/protocol/*.c edgedb/protocol/*.html
	rm -fr edgedb/protocol/*.so build *.egg-info
	rm -fr edgedb/protocol/codecs/*.html
	find . -name '__pycache__' | xargs rm -rf


compile:
	$(PYTHON) setup.py build_ext --inplace


debug:
	EDGEDB_DEBUG=1 $(PYTHON) setup.py build_ext --inplace


test:
	PYTHONASYNCIODEBUG=1 $(PYTHON) setup.py test
	$(PYTHON) setup.py test
	USE_UVLOOP=1 $(PYTHON) setup.py test


testinstalled:
	cd /tmp && $(PYTHON) $(ROOT)/tests/__init__.py
	cd /tmp && USE_UVLOOP=1 $(PYTHON) $(ROOT)/tests/__init__.py


quicktest:
	$(PYTHON) setup.py test


htmldocs:
	$(PYTHON) setup.py build_ext --inplace
	$(MAKE) -C docs html