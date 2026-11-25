# Upstream Agent Skills

The skill directories are unmodified copies of the pinned upstream directories.
The repository licenses are included alongside them.

| Skill | Upstream snapshot | License |
| --- | --- | --- |
| `dd-checklist` | [Anthropic financial-services](https://github.com/anthropics/financial-services/tree/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/private-equity/skills/dd-checklist) | [Apache 2.0](LICENSE-anthropic.txt) |
| `initiating-coverage` | [Anthropic financial-services](https://github.com/anthropics/financial-services/tree/3e32c2287dcb637c184c01cc0ad82d6af7e9bb0f/plugins/vertical-plugins/equity-research/skills/initiating-coverage) | [Apache 2.0](LICENSE-anthropic.txt) |
| `deep-research` | [moonlight-lupin agent-skills](https://github.com/moonlight-lupin/agent-skills/tree/2a599fb4f3201acb86f2266bcd197b08ee0cbd8f/research/deep-research) | [MIT](LICENSE-moonlight-lupin.txt) |

The previously referenced [Foundry Deep Research snapshot](https://github.com/foundry-works/foundry-research/blob/16249598774c2d284ba7725f112326c055b29c0d/skills/deep-research/SKILL.md) uses a YAML list for `allowed-tools`. ADK 2.15 validates this field as a string. The imported deep-research snapshot validates with the native loader as published.

ADK provides skill discovery and read-only instruction and resource loading inside the investigation graph. The upstream directories also retain their original scripts and tests for provenance.
