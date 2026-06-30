# Browser Agent Runners

Repo nay co cac entrypoint chinh:

- `server.py`: backend API cho Tampermonkey/browser bridge.
- `main.py`: runtime chinh hien tai cho workflow role-map JSON nhe; logic runtime nam trong `apps/`.
- `agents.py`: legacy runner cho solo, multi-agent, va MANAGER-first team flow.
- `solo.py`: legacy wrapper gon cho single-agent loop.
- `run.ipynb`: notebook chuan de chay va audit tung block.

Project files chinh:

- `AGENTS.md`: instruction file duy nhat o repo root.
- `prompts/HANDOFF.md`: handoff guide duoc inject cung prompt role khi can.
- `apps/`: implementation cua runtime `main.py` (`coordinator`, `route_executor`, `bridge`, `prompts`, `routing`, `models`).
- `temps/`: artifact tam thoi, ignored by git.

Root `.py` files con lai deu la entrypoint, legacy runtime, bridge, hoac helper dang duoc tests/docs tham chieu. Khong move vao `temps/` neu chua retire/update caller tuong ung.

## main.py

`main.py` la runtime chinh hien tai. File root `main.py` chi la shim; logic nam trong `apps/`.

Runtime mac dinh dung `--role` lam nguon su that chinh:

- `--role ABCD`: browser dang ky role `ABCD`, runtime tim `prompts/ABCD.txt`; neu khong co prompt thi chay goal-only mode.
- `--role A,B`: browser dang ky 2 role `A` va `B`, start role la `A`.
- `--role dev1,devx,dev99`: browser dang ky 3 role rieng `DEV1`, `DEVX`, `DEV99`; neu khong co prompt exact thi deu fallback ve prompt type `DEV`.
- Khong can truyen them `--start-role` hay `--browser-roles` trong flow binh thuong.

Ben trong runtime co 2 khai niem:

- Logical role: role xuat hien trong route JSON, vi du `MANAGER`, `PLAN`, `DEV`, `REVIEW`, `A`, `B`.
- Browser role: tab/browser worker that su trong Tampermonkey bridge, vi du tab co role `DEV` hoac `REVIEW`.

Mac dinh logical role va browser role cung ten. `--role-map`, `--prompt-roles`, `--browser-roles`, `--start-role` chi la advanced override khi ban co ly do ro rang de tach logical role khoi physical tab.

### Dieu kien truoc khi chay

- Tampermonkey bridge/server dang chay va expose API mac dinh o `http://127.0.0.1:8500`.
- Moi browser tab can co physical role dung voi `--role`.
- Prompt/skill cho role la optional:
  - prompt: `prompts/<ROLE>.txt`
  - skill: `skills/<ROLE>.md`
- Neu prompt role khong ton tai, runtime khong inject `AGENTS.md`, `prompts/HANDOFF.md`, role prompt, hay skill; no chi gui goal + continue instruction cho role do.
- Runtime inject lazy theo role: moi role chi nhan `AGENTS.md`, `prompts/HANDOFF.md`, prompt/skill cua chinh role do, goal/state/handoff hien tai.

### Lenh co ban

```powershell
uv run python main.py --role DEV --goal "noi dung task"
```

Tuong duong truyen goal bang positional args:

```powershell
uv run python main.py --role DEV "noi dung task"
```

Neu khong co `--goal` va khong co positional goal, script se doc goal tu stdin/interactive prompt.

### Mac dinh

Gia tri mac dinh hien tai:

```text
base-url      = http://127.0.0.1:8500
role          = unset
prompt-roles  = MANAGER,PLAN,DEV,REVIEW,AUDIT,A,B when --role is unset
browser-roles = DEV,REVIEW when --role is unset
manager-role  = MANAGER
start-role    = MANAGER
finish-roles  = MANAGER
max-turns     = 30
timeout       = 180s
```

Neu co `--role`, runtime lay role do lam browser roles, prompt roles, va role dau tien lam start role. Vi du `--role A,B` nghia la web dang ky hai role `A` va `B`, start role la `A`.

Neu khong co `--role`, runtime dung defaults legacy va bat dau o `MANAGER`. Neu route hop le, runtime se tiep tuc goi role tiep theo cho toi khi gap `FINISH`, het `max-turns`, hoac loi khong recover duoc.

### Route JSON bat buoc

Moi response cua role phai ket thuc bang exactly one fenced JSON object:

```json
{"DEV":"hay implement tiep phan nay..."}
```

Khong dung format cu:

```json
{"target":"DEV","reason":"x","message":"y"}
```

Rules:

- Key la role name: `MANAGER`, `PLAN`, `DEV`, `REVIEW`, `AUDIT`, `A`, `B`, hoac `FINISH`.
- Value la message string, khong gioi han do dai.
- `command` la metadata key, khong phai role.
- `command` hop le: `none`, `handoff`.
- Mot role key = handoff binh thuong.
- Nhieu role key = parallel dispatch.
- Neu `MANAGER` active, chi `MANAGER` duoc route nhieu role cung luc.
- `FINISH` khong duoc di kem role key khac hoac `command`.
- `FINISH` chi hop le neu role hien tai co finish authority.

Vi du handoff thuong:

```json
{"REVIEW":"Review implementation and evidence. Report blockers or pass criteria."}
```

Vi du parallel dispatch tu `MANAGER`:

```json
{
  "DEV":"Implement X independently. Report back to MANAGER.",
  "REVIEW":"Review current diff independently. Report back to MANAGER."
}
```

Vi du finish:

```json
{"FINISH":"TASK COMPLETE. Evidence: tests pass and smoke run completed."}
```

Vi du request handoff/reset:

```json
{
  "DEV":"Continue from HANDOFF and implement next step.",
  "command":"handoff"
}
```

Neu co `command:"handoff"`, response cung phai co `HANDOFF:` block. Runtime van ap policy/threshold truoc khi reset/new-chat.

### Pipeline moi turn

Voi moi role duoc goi:

1. Chon browser role tu `--role`; neu dung advanced flags thi chon bang `--role-map`, role cung ten, hoac round-robin trong `--browser-roles`.
2. Neu role chua tung nhan system prompt trong process hien tai va khong co `--resume`, inject system prompt cua role do.
3. Gui prompt/message vao browser role.
4. Doi `WAIT_ASSISTANT_DONE`.
5. Neu timeout hoac nghi van dang response, runtime recover bang transcript/reload logic.
6. Doc full assistant response.
7. Parse route JSON cuoi response.
8. Neu JSON sai contract, gui repair prompt va parse lai.
9. Neu route hop le, dispatch role tiep theo, parallel roles, handoff, hoac finish.

Tu turn 2 tro di, prompt gui cho role moi khong nhoi lai tat ca prompt. No chi gom goal, caller, routed message JSON, route contract, va system prompt neu day la lan dau role do xuat hien trong process.

### Resume

Dung khi tab hien tai da co response can xu ly, khong muon paste system prompt moi:

```powershell
uv run python main.py --role DEV --resume --goal "noi dung task"
```

Behavior:

- Chi ap dung o turn dau tien cua process.
- Khong gui system prompt.
- Runtime doc `last_response` hien co cua browser role duoc chon.
- Neu response dang streaming/stop button con hien, runtime wait/recover response truoc.
- Neu response co route JSON hop le, route tiep theo response do.
- Neu khong doc duoc response hop le, runtime moi gui prompt theo flow binh thuong.

Dung `--resume` khi ban vua co output trong tab va muon runtime tiep tuc route tu output do.

### 3 roles, 3 browser tabs

Khi web dang ky 3 role `DEV`, `REVIEW`, `PLAN`:

```powershell
uv run python main.py `
  --role DEV,REVIEW,PLAN `
  --finish-roles REVIEW `
  --plan-dev-handoff-every 4 `
  --goal "noi dung task"
```

Y nghia:

- Logical route chi duoc dung `DEV`, `REVIEW`, `PLAN`, `FINISH`.
- Bat dau tu `DEV`.
- `REVIEW` co quyen finish.
- Moi 4 lan `PLAN` route sang `DEV`, runtime reset/new-chat `DEV`.

### Advanced: 3 logical roles, 2 browser tabs

Khi chi co 2 tab physical `DEV`, `REVIEW`, nhung van muon logical `PLAN`:

```powershell
uv run python main.py `
  --prompt-roles DEV,REVIEW,PLAN `
  --browser-roles DEV,REVIEW `
  --role-map PLAN=REVIEW DEV=DEV REVIEW=REVIEW `
  --start-role DEV `
  --finish-roles REVIEW `
  --plan-dev-handoff-every 4 `
  --goal "noi dung task"
```

O day logical `PLAN` chay tren physical tab `REVIEW`, nhung prompt role van la `PLAN`.

### A/B/C workflow

Neu web dang ky role `A`, `B`, `C`:

```text
prompts/A.txt optional
prompts/B.txt optional
prompts/C.txt optional
skills/A.md optional
skills/B.md optional
skills/C.md optional
```

Chay:

```powershell
uv run python main.py `
  --role A,B,C `
  --finish-roles C `
  --goal "noi dung task"
```

Neu chi co 2 physical tabs:

```powershell
uv run python main.py `
  --prompt-roles A,B,C `
  --browser-roles A,B `
  --role-map A=A B=B C=B `
  --start-role A `
  --finish-roles C `
  --goal "noi dung task"
```

### Single role

Chay mot role duy nhat, role do tu lap cho den khi emit `FINISH`:

```powershell
uv run python main.py `
  --role DEV `
  --goal "lam task nay den khi xong"
```

Neu role co prompt trong `prompts/`, runtime se dung prompt do. Vi du `DEV` dung:

```text
prompts/DEV.txt
skills/DEV.md
```

### Role type fallback

Role se thu exact prompt/skill truoc. Neu khong co exact file, runtime fallback ve role type co prompt trong `prompts/`:

```text
DEV1 -> prompts/DEV1.txt, neu khong co thi prompts/DEV.txt
DEVX -> prompts/DEVX.txt, neu khong co thi prompts/DEV.txt
DEV99 -> prompts/DEV99.txt, neu khong co thi prompts/DEV.txt
REVIEW1 -> prompts/REVIEW1.txt, neu khong co thi prompts/REVIEW.txt
REVIEW_ALPHA -> prompts/REVIEW_ALPHA.txt, neu khong co thi prompts/REVIEW.txt
```

Tuong tu cho cac prompt type khac co file trong `prompts/`, vi du `PLAN*`, `AUDIT*`, `MANAGER*`.

Vi du browser co 3 role rieng `DEV1`, `DEVX`, `DEV99`, nhung ca 3 dung chung prompt/skill `DEV` neu khong co file exact:

```powershell
uv run python main.py `
  --role dev1,devx,dev99 `
  --goal "lam task nay den khi xong"
```

Luu y: prompt type 1 ky tu nhu `A`/`B` chi fallback cho exact hoac numbered role nhu `A1`; `ABCD` khong bi map nham ve `A`.

### Unknown role without prompt

Neu role khong co prompt hop le, vi du `ABCD` khong co `prompts/ABCD.txt`, runtime van chay. Khi do no khong inject `AGENTS.md`, `prompts/HANDOFF.md`, role prompt, hay skill. Prompt gui moi luot chi la goal + English continue instruction:

````text
GOAL:
...

Continue working until the goal is fully achieved. If the goal is not fully achieved yet, continue the work and do not stop. When the goal is fully achieved, end your response with exactly this fenced JSON route so the runtime can finish:
```json
{"FINISH": "TASK COMPLETE. Evidence: ..."}
```
````

Chay unknown role:

```powershell
uv run python main.py `
  --role ABCD `
  --goal "lam task nay den khi xong"
```

`--role ABCD` nghia la browser da dang ky role `ABCD`. Runtime se tim `prompts/ABCD.txt`; neu khong co thi chay goal-only mode.

Neu response cua unknown role khong co route JSON hoac co route nhung khong co `FINISH`, runtime se tiep tuc goi lai cung role cho den khi:

- role emit `FINISH`,
- het `--max-turns`,
- hoac browser/bridge loi.

### Parallel dispatch

Chi nen dung khi task doc lap. Mac dinh `MANAGER` moi duoc return multi-route neu `MANAGER` nam trong `--prompt-roles`.

Vi du:

```powershell
uv run python main.py `
  --role MANAGER,DEV,REVIEW,AUDIT `
  --goal "audit va fix"
```

Neu `MANAGER` return:

```json
{
  "DEV":"Fix implementation. Report back to MANAGER.",
  "AUDIT":"Audit independently. Report back to MANAGER."
}
```

Runtime se:

1. Dispatch `DEV` va `AUDIT` song song.
2. Wait tat ca child roles xong.
3. Gom response vao message `PARALLEL_RESULTS`.
4. Goi lai `MANAGER` de quyet route tiep.

`--parallelism N` gioi han so worker parallel, mac dinh `4`.

### Handoff/reset/new chat

Cac option lien quan:

```powershell
--new-chat-on-handoff
--handoff-command-policy auto|always|off
--min-turns-before-reset 4
--handoff-state-chars 24000
--handoff-response-chars 12000
--handoff-every-turns 0
--plan-dev-handoff-every N
```

Behavior:

- `command:"handoff"` chi la request, khong phai lenh tuyet doi.
- Neu policy `off`, runtime bo qua.
- Neu policy `always`, runtime reset khi co `HANDOFF:` block.
- Neu policy `auto`, runtime chi reset khi dat threshold: turn count, response length, state length, hoac every-N-turns.
- `--new-chat-on-handoff` reset role khi role do da co saved handoff va qua `--min-turns-before-reset`.
- `--plan-dev-handoff-every N` ep reset `DEV` moi N lan `PLAN -> DEV`.

### Reload role vua route xong

Dung de tranh tab cu tiep tuc o trang thai stale/busy sau khi da route sang role khac:

```powershell
uv run python main.py --reload-after --goal "noi dung task"
uv run python main.py --reload-after 2 --goal "noi dung task"
```

- `--reload-after` khong co value = reload sau 5s.
- `--reload-after 2` = reload sau 2s.
- Neu browser role do duoc dung lai truoc khi den gio reload, reload se bi cancel.

### Preflight

Kiem tra bridge command truoc khi chay:

```powershell
uv run python main.py --preflight --goal "noi dung task"
```

Preflight goi cac command cho moi browser role:

- `PROBE`
- `RELOAD_PAGE`
- `NEW_CHAT`

Timeout rieng:

```powershell
uv run python main.py --preflight --preflight-timeout 30 --goal "noi dung task"
```

### Timeout va state size

```powershell
--timeout 180
--request-timeout 120
--max-state-chars 30000
```

- `--timeout`: timeout cho browser action/assistant wait.
- `--request-timeout`: timeout HTTP request toi local bridge.
- `--max-state-chars`: gioi han state compact trong repair/handoff context.

### Dry run va self-test

Dry run khong goi browser bridge; runtime dung synthetic response de test flow:

```powershell
uv run python main.py --dry-run --max-turns 6 --goal "smoke route flow"
```

Self-test chay cac assert noi bo:

```powershell
uv run python main.py --self-test
```

Nen chay sau khi sua runtime:

```powershell
uv run python main.py --self-test
uv run python -m pytest tests/test_main_flow.py
```

### Tat ca flags

```text
goal ...                              positional goal text
--role ROLES                          main shortcut: browser roles + prompt roles; first role is start role
--goal TEXT                           goal text
--base-url URL                        bridge URL, default http://127.0.0.1:8500
--prompt-roles ROLES                  advanced override: logical roles duoc phep trong route JSON
--browser-roles ROLES                 advanced override: physical browser roles/tabs co san
--role-map MAP                        advanced override: map logical=physical, cach nhau bang space/comma
--manager-role ROLE                   manager role, default MANAGER
--start-role ROLE                     advanced override: role dau tien
--finish-roles ROLES                  roles duoc phep emit FINISH
--max-turns N                         gioi han turn, default 30
--timeout SECONDS                     timeout browser command/assistant wait
--request-timeout SECONDS             timeout HTTP toi bridge
--max-state-chars N                   max compact state chars
--parallelism N                       max parallel workers
--resume                              xu ly response hien co o turn dau, khong gui system prompt
--new-chat-on-handoff                 reset/new-chat khi handoff state da co va threshold dat
--min-turns-before-reset N            threshold turn truoc reset
--handoff-command-policy auto|always|off
--handoff-state-chars N               state length threshold cho auto handoff
--handoff-response-chars N            response length threshold cho auto handoff
--handoff-every-turns N               auto handoff moi N turns, 0 la tat
--plan-dev-handoff-every N            reset DEV moi N lan PLAN route sang DEV
--reload-after [N]                    reload previous browser role sau route
--preflight                           test bridge commands truoc khi chay
--preflight-timeout SECONDS           timeout cho preflight command
--dry-run                             synthetic run, khong dung browser
--self-test                           internal self-test
```

### Troubleshooting ngan

- `missing route JSON object`: role response khong co fenced JSON route block o cuoi.
- `old target/reason/message JSON is not accepted`: role dang dung contract cu.
- `only MANAGER may route to multiple roles`: non-manager return nhieu role khi `MANAGER` active.
- `FINISH cannot be combined with role routes`: `FINISH` phai dung mot minh.
- `command is not allowed with FINISH`: `FINISH` khong duoc co `command`.
- `no browser roles configured`: thieu `--role` hoac advanced `--browser-roles`.
- Tab dang response qua lau: runtime se wait/recover/reload theo response recovery path; co the them `--reload-after`.

## SOLO

Dung khi muon 1 agent tu tiep tuc lam viec trong cung session.

```powershell
uv run solo.py
uv run solo.py DEV
uv run solo.py DEV2
uv run solo.py --goal "sua bug X"
uv run solo.py DEV2 --goal "tiep tuc refactor tools"
```

### `solo.py DEV2` co hoi goal khong?

Khong. `DEV2` la ten role/browser slot, khong phai goal. Goal la optional context.

Khi chay:

```powershell
uv run solo.py DEV2
```

script khong hoi them goal. No doc web hien tai cua role `DEV2` roi tu quyet dinh theo state.

Neu muon gui goal ma khong bi hoi, dung:

```powershell
uv run solo.py DEV2 --goal "noi dung task"
```

### SOLO flow

- Script khong co `--resume`.
- Script khong co `--newchat`.
- Script khong hoi goal tu stdin. `--goal` chi la optional context.
- Dau tien check `stop_visible` va `composer_text_len`.
- Neu stop button dang hien hoac textarea co draft: wait, khong ghi de user.
- Neu khong bi block: doc last assistant response hien tai tren web.
- Neu co last response va response complete: dung script.
- Neu co last response nhung chua complete: gui continue prompt. Neu parse duoc routing JSON thi lay `message/target/reason`; neu khong parse duoc thi khong paste lai prose cu, chi gui continue instruction + goal/context.
- Neu khong co last response: coi la web sach/moi, gui system prompt role neu co + `goal` neu co.
- Neu `prompts/DEV2.txt` khong ton tai thi fallback sang `prompts/DEV.txt`.
- System prompt chi attach o ask dau tien cua process run.
- Neu response chua complete thi gui continue prompt ngan trong cung chat.
- Stop khi response co routing JSON hop le voi `target: "FINISH"`.

### SOLO wait behavior

- `composer_has_text`: textarea dang co draft, script wait. User phai gui draft hoac xoa draft.
- `assistant_generating`: assistant dang tra loi, script wait response moi.
- `assistant_ready`: co response hien tai, script dung response do lam dau vao.
- `empty_chat` / `idle_no_response`: co the gui prompt.

### SOLO busy model

Busy cua `solo.py` chi nen dua tren 2 tin hieu user/browser truc tiep:

```text
composer_text_len > 0  -> wait user send/clear draft
stop_visible == true   -> wait assistant finish
```

Sau khi het busy:

```text
last_response exists   -> process it; complete thi stop, incomplete thi continue
last_response empty    -> send initial system+goal prompt
```

## AGENTS

Dung khi can multi-agent va routing qua JSON.

```powershell
uv run agents.py
uv run agents.py --roles DEV,REVIEW --goal "implement feature X"
uv run agents.py --roles DEV1,DEV2,REVIEW --start-role DEV1 --goal "chia viec refactor"
uv run agents.py --roles MANAGER,DEV,REVIEW,AUDIT --start-role MANAGER --goal "audit toan repo"
uv run agents.py --team --roles DEV,REVIEW,AUDIT --goal "manager-led audit"
agents.bat --roles DEV,REVIEW --goal "fix tests"
```

### Command test 5 role: `MANAGER,T1,T2,T3,T4`

```powershell
uv run agents.py --roles MANAGER,T1,T2,T3,T4 --start-role MANAGER --goal "research task here"
```

Neu muon log ngan hon khi test, nen them gioi han turn:

```powershell
uv run agents.py --roles MANAGER,T1,T2,T3,T4 --start-role MANAGER --goal "research task here" --max-turns 8
```

### AGENTS flow

- Khong truyen `--roles`: hien menu chon role tu `prompts/`.
- Co `--roles`: danh sach role dang ky se tao `ALLOWED_TARGETS = roles + FINISH`.
- `--goal` la bat buoc theo logic runtime. Neu khong truyen thi script se hoi `Goal:` qua stdin.
- Agent chi duoc route toi role trong `ALLOWED_TARGETS`, hoac `FINISH` khi goal da hoan thanh.
- Role co so fallback prompt: `DEV1`, `DEV2` dung `prompts/DEV.txt` neu khong co prompt rieng.
- Luot dau cua moi role co the attach system prompt; cac luot sau gui goal/state/message ngan hon.
- Neu response thieu JSON hoac JSON sai target: script gui format repair prompt.
- Neu JSON loi qua gioi han: tra ve `format_blocked`.
- Chi `MANAGER` moi duoc route song song bang target phan tach boi dau phay, va khi do `reason` phai la `parallel_dispatch`.
- Neu role duoc hoi lan dau ma web dang co response cu, script van cho phep gui prompt moi; no khong coi response cu do la busy.
- State handoff giua cac role chi giu ket qua moi nhat, khong cong don toan bo lich su turn.

### JSON routing format

Agent nen ket thuc bang mot fenced JSON object:

```json
{
  "target": "REVIEW",
  "reason": "implementation da xong, can review",
  "message": "Review thay doi va chi ra bug/blocker neu co."
}
```

Neu task da xong, response phai ket thuc bang routing JSON:

```json
{"target":"FINISH","reason":"complete_verified","message":"evidence summary"}
```

Rule runtime quan trong:

- Non-`MANAGER` phai chon duy nhat 1 `target`.
- `MANAGER` moi duoc dung `target` dang `T1,T2,T3` va chi khi `reason="parallel_dispatch"`.
- Neu sai schema `target/reason/message`, script se repair toi da theo `max_format_repairs` trong `config.toml`.

## MANAGER-first team mode

Team runner da duoc merge vao `agents.py`. Dung `--team` khi muon mac dinh bat dau tu `MANAGER` va de manager dieu phoi worker.

```powershell
uv run agents.py --team --roles DEV,REVIEW --goal "sua bug va review"
uv run agents.py --team --roles DEV1,DEV2,REVIEW --goal "chia implementation cho 2 dev"
uv run agents.py --team --roles DEV,REVIEW,AUDIT --start-role REVIEW --goal "review truoc roi route"
uv run agents.py --team --roles DEV,REVIEW --goal "task X" --no-parallel
```

- Mac dinh team mode prepend/start bang `MANAGER` neu prompt `MANAGER` ton tai.
- `--no-parallel` ep format repair neu manager tra target nhieu role.
- Core workflow van la `agents.py`; khong con runner team rieng.

## Role prompt fallback

Role co hau to so se fallback ve base role:

```text
DEV1   -> prompts/DEV1.txt, neu khong co thi prompts/DEV.txt
DEV2   -> prompts/DEV2.txt, neu khong co thi prompts/DEV.txt
REVIEW2 -> prompts/REVIEW2.txt, neu khong co thi prompts/REVIEW.txt
```

Moi role/browser slot van doc lap. `DEV`, `DEV1`, `DEV2` co the cung dung prompt `DEV.txt` nhung la cac session rieng.

## Config

Mot so gia tri lay tu `config.toml` neu runner co dung config:

- `max_turns`: so turn toi da.
- `timeout_s`: timeout doi assistant.
- `sleep_s`: khoang nghi giua cac turn.
- `busy_reload_after_s`: thoi gian wait truoc khi reload de tranh state sai.

## Image + text upload bridge

The browser bridge now supports multimodal turns: text plus optional image/file artifacts. The stable path is not OS clipboard simulation. All image sources are normalized on the Python side, then sent to Tampermonkey as one `UPLOAD_FILES` command.

Supported source kinds:

- local path
- web URL
- base64 or data URL
- OS clipboard image or file list
- raw bytes from Python

Normalized upload item shape:

```json
{
  "filename": "image.png",
  "mime_type": "image/png",
  "data_b64": "...",
  "size": 12345,
  "source_kind": "local",
  "meta": {}
}
```

Default upload method is `input`, not `auto`. ChatGPT currently exposes hidden file inputs such as `#upload-files` and `#upload-photos-input`; using the input path avoids duplicate overlay bugs caused by broadcasting the same file through multiple paste/drop targets. `auto`, `paste`, and `drop` are still available only when explicitly requested.

Run a local image upload:

```powershell
cd E:\python_project\tampermonkey_auto
py upload_once.py --role IMG --local "C:\path\to\image.png" --text "Describe this image."
```

Run without sending, useful for UI checks:

```powershell
py upload_once.py --role IMG --local "C:\path\to\image.png" --text "Describe this image." --no-send
```

Upload from other sources:

```powershell
py upload_once.py --role IMG --web "https://example.com/image.png" --text "Describe this image."
py upload_once.py --role IMG --base64-file temps\image.b64 --filename image.png --mime image/png --text "Describe this image."
py upload_once.py --role IMG --clipboard --text "Describe this clipboard image."
```

Python API:

```python
import agents

agents.run_upload_files(
    "IMG",
    ["temps/example.png"],
    text="Describe this image.",
)

agents.run_upload_sources(
    "IMG",
    [{"kind": "web", "url": "https://example.com/image.png"}],
    text="Describe this image.",
)
```

Safety/robustness rules:

- Do not commit real API keys or bearer tokens. Use environment variables or local `.env` files that remain untracked.
- Upload payloads are uniquified by default to avoid ChatGPT duplicate-file guards.
- PNG uploads receive a harmless metadata chunk when uniquified; visible image content is unchanged.
- Stale upload overlays are dismissed before upload attempts.
- Test artifacts belong under `temps/`, which is ignored by git.

Recommended workflow model:

```text
task
-> build multimodal message envelope
-> normalize artifacts
-> upload artifacts if present
-> set/send prompt text
-> wait assistant response
-> capture transcript and attachment evidence
-> route next step
```

Use one turn envelope for both text-only and image+text messages. Do not maintain separate text-flow and image-flow paths.
