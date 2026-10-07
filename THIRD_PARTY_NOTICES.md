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

## This project's license status

At the time this source package was prepared, the project owner had not selected a distribution license for the newly authored EventArena code and author annotations. This notice does not grant an MIT license or any other additional rights to those materials. The owner should select and add an appropriate root `LICENSE` before representing the project as licensed open-source software. Do not assume that the upstream Mind2Web code license covers newly authored EventArena materials.

## Python and browser dependencies

Dependencies are installed separately through `requirements.txt` and Playwright's Chromium installer. This source ZIP does not bundle their environments or browser binaries. Each dependency retains its own license and notices; installation does not transfer the upstream Mind2Web data license to those dependencies. Consult the respective package distributions for applicable notices.

## Website material and run artifacts

Runtime observations, screenshots, HAR responses and page content originate from visited websites. They are not relicensed by the Mind2Web data card or by this notice. This public package excludes model credentials and runtime recordings. Before sharing a future experiment artifact, review its contained site material, session information and personal data; keep original source attribution and do not present author-added events as official dataset annotations.
