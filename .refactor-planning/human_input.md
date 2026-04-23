D1: A, StrictModel
D2: delete
D3: use that dispatch table pattern
D4: A, ok
D5: B, migrate
D6: B, rename
D7: A
D8: B — python -m 子命令都改成 epubforge editor xxx 去掉 -m editor.xx

R5: i don't care breaking that file, i've finished all job on it.
R6: why there are two toml files? we should only have one config toml?
R6 decision: don't infer config file to read, only read from the file specified in cli arg, don't make config.toml / config.local.toml as default config, force user to specify.

discarded items but i think it should be done:
item 7: i prefer to use op_log_version
