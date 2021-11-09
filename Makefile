CONTAINER_COMMAND := $(shell ./utils.sh get_container_runtime_command)
TAG := $(or ${TAG},latest)
ASSISTED_EVENTS_SCRAPE_IMAGE := $(or $(ASSISTED_EVENTS_SCRAPE_IMAGE),quay.io/edge-infrastructure/assisted-events-scrape:$(TAG))

install_assisted_service_client:
	python3 -m pip install assisted-service-client

build-image:
	$(CONTAINER_COMMAND) build -t $(ASSISTED_EVENTS_SCRAPE_IMAGE) .

build-wheel:
	rm -rf ./dist ./build
	python3 -m setup bdist_wheel

install: build-wheel
	python3 -m pip uninstall assisted_events_scrap -y
	python3 -m pip install -I dist/assisted_events_scrap-*-py3-none-any.whl


##########
# Verify #
##########

lint: flake8

flake8:
	flake8 .
