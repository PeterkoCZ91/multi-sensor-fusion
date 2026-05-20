## Type of Change

<!-- Check the one that applies -->

- [ ] Bug fix
- [ ] New feature
- [ ] Refactoring (no functional change)
- [ ] Documentation
- [ ] CI / build / tooling
- [ ] Other: ___

## Description

What does this PR do and why?

## Testing

<!-- Describe how you tested the change -->

- [ ] `docker compose build` completes without errors
- [ ] `docker compose up -d` starts the service successfully
- [ ] MQTT messages from sensors are received and processed (check `docker compose logs -f fusion`)
- [ ] Home Assistant entities update as expected (if HA integration is affected)

## Checklist

- [ ] No hardcoded credentials, IP addresses, or tokens anywhere in the diff
- [ ] `config.yaml.template` updated if new configuration keys were added
- [ ] `CHANGELOG.md` entry added
- [ ] `docker-compose.yml` changes are backward compatible (no removal of existing keys)
- [ ] `requirements.txt` updated if new dependencies were added

## Related Issues

Closes #
