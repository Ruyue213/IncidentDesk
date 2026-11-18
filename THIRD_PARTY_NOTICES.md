# Third-party software and model notices

IncidentDesk's original code is Apache-2.0. That license does not replace the licenses of dependencies, bundled frontend code, or optional model files. Python and npm dependencies are identified by `uv.lock` and `frontend/package-lock.json`; installed distributions retain their own license files and notices. The table below highlights components relevant to this release and is not an exhaustive inventory of transitive dependencies.

| Component | Version/reference | License and authoritative source | Distribution/use |
| --- | --- | --- | --- |
| Multilingual E5 small | `intfloat/multilingual-e5-small` at `fd1525a9fd15316a2d503bf26ab031a61d056e98` | MIT, declared by the [pinned upstream model card](https://huggingface.co/intfloat/multilingual-e5-small/blob/fd1525a9fd15316a2d503bf26ab031a61d056e98/README.md) | Optional explicit download; weights are not included in Git, wheel, sdist, or state backups |
| Multilingual MiniLM Cross-Encoder | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` at `1427fd652930e4ba29e8149678df786c240d8825` | Apache-2.0, [pinned upstream model card](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1/blob/1427fd652930e4ba29e8149678df786c240d8825/README.md) | Optional explicit download. Weights are excluded from source, distributions and backups. |
| Sentence Transformers | 5.7.0 in the current lock | [Apache-2.0 and upstream notices](https://github.com/huggingface/sentence-transformers/tree/v5.7.0) | Optional `rag` dependency; its distribution includes LICENSE and NOTICE.txt |
| PyTorch | 2.15.0 in the current lock | [BSD-style core license and additional third-party licenses](https://github.com/pytorch/pytorch/blob/v2.15.0/LICENSE) | Optional `rag` dependency; preserve its full packaged license files when redistributing |
| jieba | 0.42.1 | [MIT](https://github.com/fxsjy/jieba/blob/master/LICENSE) | Python lexical tokenization dependency |
| Deep Agents | 0.7.34 | [MIT](https://github.com/langchain-ai/deepagents) | Python agent dependency |
| LangChain / LangGraph | 1.4.9 / 1.2.20 in the current lock | [LangChain](https://github.com/langchain-ai/langchain) and [LangGraph](https://github.com/langchain-ai/langgraph), MIT | Python orchestration dependencies |
| React / React DOM | 19.3.1 | [MIT](https://github.com/facebook/react/blob/main/LICENSE) | Compiled into the distributed web UI; full notices retained in `third_party/licenses/` and packaged UI resources |
| React Scheduler | 0.28.0 | [MIT](https://github.com/facebook/react/blob/main/LICENSE) | Transitive production UI code; full notice retained alongside React's |

PyTorch's installed 2.15.0 metadata declares the combined license expression `Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND ISC AND MIT AND Zlib`. Describing the whole installed package as merely “BSD” would omit bundled third-party terms. The package's `License-File` entries carry the relevant texts; IncidentDesk does not vendor PyTorch itself.

The optional E5 setup retrieves only the fixed public model revision, tokenizer/configuration files, and its model card. Review the upstream terms before redistributing those files. A downloaded embedding model is independent of IncidentDesk's Apache-2.0 source license.

Third-party application source, private project data, and external integration patches are excluded from this public repository. References to a service or protocol do not grant a license to its code. If adding vendored code, datasets, fonts, models, or generated assets, record the source/version and retain all notices required by that artifact's license.
