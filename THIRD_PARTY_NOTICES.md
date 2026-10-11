# Third-party sources and notices

## Mind2Web dataset

The source task corpus used to construct this project's author candidates is **Mind2Web**, released by the OSU NLP Group:

- Dataset: <https://huggingface.co/datasets/osunlp/Mind2Web>
- Official repository: <https://github.com/OSU-NLP-Group/Mind2Web>
- Project: <https://osu-nlp-group.github.io/Mind2Web/>
- Paper: <https://arxiv.org/abs/2306.06070>
- Dataset license: **Creative Commons Attribution 4.0 International (CC BY 4.0)**, as stated in the [official dataset card](https://huggingface.co/datasets/osunlp/Mind2Web/blob/main/README.md).
- License text: <https://creativecommons.org/licenses/by/4.0/>
- Pinned dataset revision: `17ece8eb89862368edc0cc806acee6fca5163474`.

The official repository separately asks users not to redistribute the unzipped test data online in order to prevent benchmark contamination. This public source package therefore includes identifiers and checksums, author-defined adaptations and event construction recipes; original task text, test JSON and HTML are restored locally from official downloads. Raw, normalized and runtime dataset directories are excluded from Git tracking. See [data setup](docs/DATA_SETUP.md) and the official repository's [Dataset Access](https://github.com/OSU-NLP-Group/Mind2Web#dataset-access).

作者新增内容包括任务适配、突发事件、候选标签、检查条件和实验实现。这些内容并非 Mind2Web 作者提供的事件 benchmark。原任务文本仅在本地恢复，适配任务明确记录修改；本项目不是官方 Mind2Web 或 Online-Mind2Web 的等价复现。

Citation supplied by the official dataset card:

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

## Official Mind2Web code

The official Mind2Web code repository uses the **MIT License**, with `Copyright (c) 2023 OSU Natural Language Processing`. Its license can be read at <https://github.com/OSU-NLP-Group/Mind2Web/blob/main/LICENSE>. Copies or substantial portions of that software must retain its copyright and permission notice.

This project does not require the official repository's model-training implementation or a `vendor/Mind2Web` checkout to run the v2 online experiments. Its upstream MIT license applies to that upstream software; it does **not** replace the separate CC BY 4.0 license of the Mind2Web data, and it does not automatically license this project's newly authored code.

## Official Online-Mind2Web task source

The current official event extension uses the task file `Online_Mind2Web.json` from [osunlp/Online-Mind2Web](https://huggingface.co/datasets/osunlp/Online-Mind2Web), with the task goals and identifiers retained exactly. The upstream project is [OSU-NLP-Group/Online-Mind2Web](https://github.com/OSU-NLP-Group/Online-Mind2Web). This release was constructed using revision `eacad896a84dc5b65e29b0b06e4699ab0544d701`, raw SHA256 `7dedf381531d423dc0fc48d21dc1d425655dd75f38b10211a880fa09102f257f`.

The official task file is gated and is **not redistributed** in this public package. Users must obtain it through the official access workflow and comply with its terms. The author-defined event recipes in `config/official_online/` refer to official task IDs; compilation obtains the source task text from the user's local authorized corpus. The project's MIT license does not relicense official task data, website content, or third-party software. The extension's scores are not official leaderboard scores.

## This project's license

The existing [yangkf825/EventPilot](https://github.com/yangkf825/EventPilot) repository contains an MIT License, copyright (c) 2026 yangkf825. This update preserves that owner's existing root `LICENSE` for newly authored project code. Separate upstream data terms and third-party notices remain applicable; the Mind2Web code license must not be mistaken for the Mind2Web data license.

## Python and browser dependencies

Dependencies are installed separately through `requirements.txt` and Playwright's Chromium installer. This source ZIP does not bundle their environments or browser binaries. Each dependency retains its own license and notices; installation does not transfer the upstream Mind2Web data license to those dependencies. Consult the respective package distributions for applicable notices.

## Website material and run artifacts

Runtime observations, screenshots, HAR responses and page content originate from visited websites. They are not relicensed by the Mind2Web data card or by this notice. This public package excludes model credentials and runtime recordings. Before sharing a future experiment artifact, review its contained site material, session information and personal data; keep original source attribution and do not present author-added events as official dataset annotations.
