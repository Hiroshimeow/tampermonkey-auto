# HANDOFF.md

Use this guide when a role needs context reset or a new chat before the next phase.

## How A Role Requests Handoff

A role requests handoff by doing both things in the same response:

1. include a `HANDOFF:` section with enough state to continue safely,
2. include `"command":"handoff"` in the final route JSON.

Minimal JSON example:

```json
{
  "DEV":"Continue from the handoff and implement the next phase.",
  "command":"handoff"
}
```

Full response example:

````text
RESULT:
Phase 1 is complete. The next role needs a clean session because the current context is large.

HANDOFF:
Goal: Build the requested workflow.
Current phase: DEV implemented the route parser and command policy.
Files touched: main.py, AGENTS.md, HANDOFF.md.
Evidence: main.py self-test passed.
Remaining work: REVIEW should check edge cases around command=handoff and non-manager flows.
Risks: legacy tests may still expect target/reason/message.
Next instruction: Continue from this handoff and review the implementation.

```json
{
  "REVIEW":"Continue from HANDOFF and review the handoff command implementation.",
  "command":"handoff"
}
```
````

`command` is metadata, not a route role.

## Runtime Policy

The runtime treats `command: handoff` as a request.

Default policy is `auto`: reset happens only when the response contains `HANDOFF:` and at least one reset condition is met:

- turn count is at or above `--min-turns-before-reset`,
- response length is at or above `--handoff-response-chars`,
- compact state length is at or above `--handoff-state-chars`,
- `--handoff-every-turns N` divides the current turn.

Other policies:

- `--handoff-command-policy always`: reset on request if `HANDOFF:` exists.
- `--handoff-command-policy off`: ignore handoff commands.

## Good Handoff Content 

Hãy tạo một bản handoff/compressed context để chuyển toàn bộ công việc hiện tại sang một session/agent mới.
Mục tiêu: agent mới phải có thể tiếp tục công việc ngay từ trạng thái hiện tại, không cần đọc lại toàn bộ conversation cũ và không hỏi lại những thứ đã được quyết định.
Hãy viết handoff bằng cấu trúc sau:
# Handoff cho session mới
## 1. Mục tiêu tổng thể
* Người dùng đang muốn đạt được điều gì?
* Kết quả cuối cùng mong muốn là gì?
* Phạm vi công việc là gì, không bao gồm gì?
## 2. Bối cảnh quan trọng
* Tóm tắt ngắn gọn tình huống, dự án, domain, nhân vật, hệ thống, hoặc sản phẩm liên quan.
* Những thông tin nền nào agent mới bắt buộc phải biết?
* Những giả định nào đang được dùng?
## 3. Những gì đã làm
Liệt kê theo thứ tự:
* Các bước đã hoàn thành.
* Kết quả từng bước.
* Quyết định đã đưa ra.
* Lý do chính của các quyết định đó.
## 4. Trạng thái hiện tại
* Công việc đang dừng ở đâu?
* Output hiện tại là gì?
* Có file, đoạn code, tài liệu, bảng, prompt, cấu hình, link, command, log, hoặc artifact nào đang quan trọng không?
* Nêu rõ đường dẫn file/tên file/nội dung chính nếu có.
## 5. Yêu cầu và ràng buộc của người dùng
Ghi rõ:
* Ngôn ngữ trả lời.
* Tone/style mong muốn.
* Format mong muốn.
* Các điều người dùng đã yêu cầu tránh.
* Các tiêu chí chất lượng.
* Các constraint kỹ thuật, pháp lý, bảo mật, deadline, nền tảng, tool, version nếu có.
## 6. Việc còn phải làm
Tạo checklist rõ ràng:
* [ ] Việc tiếp theo cần làm ngay.
* [ ] Các bước sau đó.
* [ ] Những phần cần kiểm tra lại.
* [ ] Những phần cần hoàn thiện.
* [ ] Những điểm chưa chắc chắn.
## 7. Hướng dẫn tiếp tục cho agent mới
Viết chỉ dẫn cụ thể:
* Agent mới nên bắt đầu từ đâu.
* Nên dùng cách tiếp cận nào.
* Không nên làm lại những gì.
* Không nên hỏi lại những thông tin nào đã có.
* Khi nào cần hỏi người dùng.
* Nếu thiếu dữ liệu thì nên xử lý ra sao.
## 8. Rủi ro / điểm dễ sai
* Những chỗ có khả năng hiểu nhầm.
* Những quyết định có thể bị đảo ngược nếu có thêm thông tin.
* Những phần cần đặc biệt cẩn thận.
## 9. Tóm tắt cực ngắn
Viết 5–10 dòng cô đọng nhất để agent mới đọc nhanh trước khi làm.
Yêu cầu chất lượng:
* Ưu tiên đầy đủ hơn ngắn gọn.
* Không bỏ sót quyết định hoặc constraint quan trọng.
* Không viết chung chung.
* Không chỉ summarize; phải preserve actionable state.
* Dùng bullet rõ ràng.
* Nếu có thông tin chưa chắc, đánh dấu là “chưa chắc” thay vì đoán.
* Kết thúc bằng “Next action recommended:” và nêu đúng hành động tiếp theo agent mới nên làm.
* Viết handoff như tài liệu vận hành để một người khác tiếp quản công việc ngay lập tức.
## Rules

- Do not request handoff without a `HANDOFF:` section.
- Do not use handoff with `FINISH`.
- Handoff can be requested by any active role; it is not manager-only.
- When `MANAGER` is active, only `MANAGER` may dispatch multiple roles in one JSON object.
- Without `MANAGER`, small flows such as `PLAN,DEV,REVIEW` may still request handoff normally.