OVERLAYS := apps/live-commerce/overlays/dev apps/live-commerce/overlays/prod \
            apps/moderation/overlays/dev apps/moderation/overlays/prod platform/edge

.PHONY: validate lint build
validate: lint build

lint:
	yamllint .

build:
	@for o in $(OVERLAYS); do \
	  echo "==> $$o"; \
	  kustomize build $$o | kubeconform -strict -summary -ignore-missing-schemas - || exit 1; \
	done
