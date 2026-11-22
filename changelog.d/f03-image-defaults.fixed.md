- **Hosted container starts no longer replace service.env with image defaults.**
  On Railway and with `UA_HOSTED=1`, the entrypoint restored the whole start
  environment after sourcing `service.env`, so a key that was also a Dockerfile
  default (such as `UA_EDITION` or `UA_WORKSPACE`) came back as the baked value.
  `service.env` and a real runtime value now win over that default; the default
  applies only when the key is unset.
