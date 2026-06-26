# Browser Agent Runners

Repo nay co 3 entrypoint chinh:

- `solo.py`: chay 1 agent trong 1 browser/session, khong can JSON routing.
- `agents.py`: multi-agent routing bang JSON, dung `ALLOWED_TARGETS`.
- `teams.py`: ban thu nghiem manager-led, de dung hon khi co nhieu worker.

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
uv run agents.py --roles MANAGER,DEV,REVIEW,AUDIT --goal "audit toan repo"
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

## TEAMS

`teams.py` la runner thu nghiem theo huong manager-led.

```powershell
python teams.py --roles DEV,REVIEW --goal "sua bug va review"
python teams.py --roles DEV1,DEV2,REVIEW --goal "chia implementation cho 2 dev"
python teams.py --roles DEV,REVIEW,AUDIT --start-role REVIEW --goal "review truoc roi route"
python teams.py --roles DEV,REVIEW --goal "task X" --no-parallel
```

### TEAMS flow

- Mac dinh start bang `MANAGER`.
- Worker lay tu `--roles`.
- Manager co the route song song toi nhieu worker.
- `--no-parallel` ep repair neu manager tra target nhieu role.
- Van dung JSON routing, nhung model lam viec don gian hon: manager chia viec, worker lam, manager tong hop.

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
