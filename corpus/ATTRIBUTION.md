# Data attribution and transformation record — EventPilot v11

This is the local recorded-page companion for the 460 reviewed EventPilot cases.
It is a selected subset, not the complete original Mind2Web distribution or a
website backend. No endorsement by the upstream authors is implied.

- Upstream: **Mind2Web: Towards a Generalist Agent for the Web** (2023).
- Project: https://osu-nlp-group.github.io/Mind2Web/
- Repository: https://github.com/OSU-NLP-Group/Mind2Web
- Paper: https://arxiv.org/abs/2306.06070
- The locally supplied upstream README identifies its dataset license as CC BY 4.0;
  the original README is preserved at `upstream/Mind2Web_README.md`.
- License reference: https://creativecommons.org/licenses/by/4.0/

The normalized task index is preserved byte for byte (1,750 index rows). The
`snapshots.zip` archive contains the 2,197 unchanged cleaned HTML snapshots needed
by the 362 distinct source tasks referenced in this release. Every required HTML
is checked against its frozen SHA256. The source metadata catalog retains the
previous 560-task selection; unused metadata entries are not new benchmark cases.
The archive packaging changes storage only, not source text, node IDs, action
values, source labels, or recorded transitions. Raw HTML and unselected source
assets from the complete upstream distribution are not included.

EventPilot's events, labels, task orchestration, and experiment registration are
author additions. They are not notifications collected from the original sites.
The reviewed workbook was approved by the project owner; no independent
inter-annotator agreement or model performance is asserted by this data package.
Evaluation is offline and teacher-forced. It does not verify website submissions
or arbitrary backend outcomes.

```bibtex
@misc{deng2023mind2web,
  title={Mind2Web: Towards a Generalist Agent for the Web},
  author={Xiang Deng and Yu Gu and Boyuan Zheng and Shijie Chen and Samuel Stevens and Boshi Wang and Huan Sun and Yu Su},
  year={2023},
  eprint={2306.06070},
  archivePrefix={arXiv},
  primaryClass={cs.CL}
}
```
