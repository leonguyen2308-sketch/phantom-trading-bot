# Grok Bot: copy trade ban đêm giống video

Bot này làm lại cách chạy trong video "Grok Bot × copyfomo": ban đêm (22:00 → 08:00) copy 2 trader đứng đầu bảng xếp hạng tuần, trader nào thua 2 lệnh liền thì Grok đổi người, và điện thoại nhận thông báo từng lệnh. Bot dùng **Grok thật** (API của xAI) để chọn trader và đọc X (Twitter) về từng coin.

> **Đọc trước:** video là **quảng cáo của copyfomo** (logo copyfomo nằm ngay trên widget). Con số $115 → $2.145 trong một đêm (x18,7, thắng 82%) là hình minh hoạ quảng cáo, không phải sao kê tài khoản thật. Theo DWF Ventures, khoảng **94% ví copy trade trên FOMO lỗ** sau 3 tháng. Bot này làm đúng *cách chạy* trong video, nhưng không ai làm được *kết quả* đó theo ý muốn. Hãy chạy tiền giả trước.

---

## Bot làm gì

| Trong video | Bot này |
|---|---|
| "Night copy window open · 22:00 → 08:00" | Chỉ mở lệnh copy mới từ 22:00 tới 08:00 (theo giờ máy Mac). Ngoài giờ đó, coin đang giữ vẫn được bán khi trader bán hoặc chạm cắt lỗ. |
| "Following Oura456 (#1) and BtheBezel (#2)" | Copy **2 trader cùng lúc**, lấy từ top tuần. Grok xếp lại thứ tự trước khi chọn. |
| "318 SOL in. Mirrored with 0.4 SOL · size inside the risk cap" | Mỗi lệnh = 0,25 lần lệnh của trader, nhưng **không quá 25% tài khoản** (video ghi 45%, xem phần Chỉnh). |
| "Big win · sold $POKEMON · exit copied 0.5s after the trader sold" | Trader bán thì bot bán đúng tỷ lệ đó. Lệnh lãi từ +50% có thông báo riêng, ghi rõ bot bán sau trader bao nhiêu giây. |
| "Copied 5 trades · 4W 1L" | Cứ 30 phút gửi một tin tổng hợp các lệnh đã đóng. |
| "Grok switched trader · took 2 losses in a row" | Trader thua 2 lệnh copy liền thì bị đuổi 7 ngày, người kế tiếp trong danh sách (theo thứ tự Grok xếp) lên thay. |
| "Copy window closed · $115 → $2,145 overnight · 41W 9L" | 08:00 gửi tóm tắt cả đêm: số dư đầu → cuối, số lệnh thắng/thua, tỷ lệ thắng. |

**Grok làm gì (thật, không chỉ là cái tên):**
1. **Xếp hạng trader:** mỗi ngày (và mỗi lần đuổi một trader) Grok xem số liệu của top tuần: hạng, lãi 7 ngày, tỷ lệ thắng, và kết quả *bot đã copy* người đó. Grok chọn ai được copy trước và ai thay khi có người bị đuổi.
2. **Đọc X về từng coin:** sau mỗi lệnh copy, Grok tìm trên X theo địa chỉ hợp đồng của coin. Nếu nhiều bài nói coin đó rug, honeypot hoặc dev xả hàng thì bot **bán ngay**. Grok chỉ thấy địa chỉ coin, không thấy tên coin (tên do người tạo coin đặt, có thể chứa câu lừa AI), và chỉ có quyền khiến bot bán sớm hơn, không bao giờ mua thêm.
3. Lệnh copy **không bao giờ phải chờ Grok**: Grok chạy song song, chậm hay lỗi thì bot vẫn copy theo luật.

Thông báo về điện thoại đi qua **Telegram** (giống hàng thông báo trên màn hình khoá trong video). Dashboard ở **http://localhost:8005** trông như màn hình khoá trong video, chỉ mở được trên máy Mac đang chạy bot.

---

## Chi phí

- **Grok (xAI):** model `grok-4.7` khoảng $2 cho 1 triệu token đọc và $6 cho 1 triệu token viết. Tìm trên X tính $5 cho 1.000 bài đọc. Một lần kiểm tra X tốn khoảng **$0,10**, một lần xếp hạng trader vài cent. Bot tự dừng dùng Grok khi tiêu hết **$2 mỗi ngày** (`grok.daily_budget_usd`), sau đó vẫn copy theo luật. Giá do xAI đặt và có thể đổi: xem lại trên console.x.ai.
- **Helius và Jupiter:** gói miễn phí như bot FOMO.

---

## Cài đặt trên Mac

Mỗi khối là các lệnh dán vào Terminal, chờ xong rồi dán khối tiếp theo.

**1. Cập nhật code:**

```
cd ~/phantom-trading-bot
git checkout -- config/desk.yaml
git pull
```

**2. Xem thử bằng mô phỏng (không cần key, không tốn gì):**

```
./grok.sh demo
```

Mở **http://localhost:8005**. Đây là một đêm giả chạy nhanh gấp 60 lần (10 giờ trong khoảng 10 phút), với trader giả, coin giả và một "Grok giả". Mô phỏng cố ý bất lợi cho người copy nên thường lỗ: số trong demo không nói gì về kết quả thật. Bấm `Ctrl + C` để dừng.

**3. Lấy key Grok (xAI):** vào [console.x.ai](https://console.x.ai), đăng nhập, vào **API Keys** → **Create API Key** → copy key. Vào **Billing** nạp ít tiền (ví dụ $5). Rồi mở `.env`:

```
open -e .env
```

Dán key vào dòng `XAI_API_KEY=` (ngay sau dấu `=`, không để cách), lưu, đóng file. Đóng file `.env` trước khi chụp màn hình.

**4. Danh sách trader top tuần:** giống bot FOMO, chọn một cách:
- `FOMOAPI_KEY` (fomoapi.io): đúng bảng xếp hạng tuần của app FOMO như trong video.
- `SOLANATRACKER_API_KEY` (solanatracker.io): top ví Solana lãi nhiều nhất 7 ngày.
- File `config/wallets.txt`: mỗi dòng một địa chỉ ví, người tốt nhất ở trên cùng.

Kiểm tra:

```
./grok.sh traders
```

**5. Thông báo về điện thoại (tuỳ chọn, nên có):** điền `TELEGRAM_BOT_TOKEN` và `TELEGRAM_CHAT_ID` trong `.env` (cách lấy ở `HUONG_DAN.md`, mục **I. Tạo bot Telegram**). Không có Telegram thì thông báo chỉ hiện trên dashboard.

**6. Chạy bằng tiền giả, chạy nền, tự khởi động lại:**

```
scripts/install-fomo-service.sh paper grok
```

Mở **http://localhost:8005**. Cắm sạc Mac và **đừng gập màn hình**: Mac ngủ thì bot không thấy lệnh. Tiền giả bắt đầu từ $115 như video (đổi bằng `GROK_PAPER_START_USD` trong `.env`). Tắt bot:

```
launchctl unload ~/Library/LaunchAgents/com.phantom.grok.plist
```

Sáng hôm sau xem thông báo **"Copy window closed"**: đó là kết quả thật của đêm đó (bằng tiền giả).

---

## Chỉnh (`config/grok.yaml`)

| Mục | Mặc định | Ý nghĩa |
|---|---|---|
| `window` | `"22:00-08:00"` | Khung giờ mở lệnh mới. `null` = cả ngày |
| `follow_count` | 2 | Copy bao nhiêu trader cùng lúc |
| `max_position_pct` | 0.25 | Mỗi lệnh tối đa 25% tài khoản. Video ghi 45%: với cắt lỗ 35%, một lệnh xấu mất khoảng 16% tài khoản (25% thì mất khoảng 9%) |
| `stop_loss_pct` | 35 | Coin giảm 35% thì bán (thường trader bán trước) |
| `fire_after_losses` | 2 | Thua 2 lệnh liền thì đổi trader |
| `close_at_window_end` | false | `true` = 08:00 bán hết những gì còn giữ |
| `grok.x_check` | true | Grok đọc X sau mỗi lệnh copy |
| `grok.daily_budget_usd` | 2.0 | Tiền Grok tối đa mỗi ngày |
| `notify.digest_minutes` | 30 | Tin tổng hợp mỗi 30 phút |
| `daily_loss_limit_pct` / `max_drawdown_pct` | 0.30 / 0.50 | Lỗ 30% trong ngày thì ngừng copy tới 00:00 UTC; giảm 50% từ đỉnh thì kill switch bán hết và dừng |

Sửa file xong thì chạy lại `scripts/install-fomo-service.sh paper grok`. Trước mỗi lần `git pull`, chạy `git checkout -- config/grok.yaml` (mất phần đã sửa, nhớ ghi lại).

---

## Tiền thật (chỉ khi tiền giả đã có lãi sau nhiều đêm)

1. Trong Phantom tạo **ví MỚI** chỉ cho bot này. Gửi vào USDC (mạng Solana), chỉ số bạn chấp nhận mất hết, và khoảng 0,1 SOL phí.
2. Trong `.env` điền `GROK_BOT_ADDRESS` và `GROK_BOT_PRIVATE_KEY` của ví mới đó. Không dán key này ở bất kỳ chỗ nào khác.
3. Chạy lần lượt, làm theo chữ hiện ra:

```
./grok.sh wallet-check
./grok.sh approve
./grok.sh ack-risks
./grok.sh golive-check
```

4. Khi `golive-check` báo mọi dòng PASS (cần ít nhất 24 giờ và 5 lệnh tiền giả), sửa `.env` thành `GROK_LIVE=yes` rồi:

```
scripts/install-fomo-service.sh live grok
```

---

## Các lệnh

| Lệnh | Làm gì |
|---|---|
| `./grok.sh demo` | một đêm mô phỏng + dashboard, không cần key |
| `./grok.sh traders` | xem top tuần, kiểm tra bot có thấy lệnh của vài trader đầu không |
| `./grok.sh paper` | copy thật, tiền giả (chạy trong cửa sổ Terminal đang mở) |
| `./grok.sh live` | tiền thật, sau khi `golive-check` đạt |
| `./grok.sh kill` | bán hết, ngừng copy |
| `./grok.sh reset` | bật lại sau kill switch |
| `scripts/install-fomo-service.sh paper grok` | chạy nền, tự bật lại sau khi khởi động máy |

Code dùng chung với bot FOMO (`fomo/`), phần Grok ở `fomo/grok.py`, dashboard ở `dashboard/grok.html`, test ở `tests/test_grok_bot.py`.

Nguồn:
- [xAI: X Search (công cụ x_search)](https://docs.x.ai/developers/tools/x-search) · [xAI: bảng giá API](https://docs.x.ai/developers/pricing) · [xAI: Structured Outputs](https://docs.x.ai/developers/model-capabilities/text/structured-outputs)
- [X Search tính tiền theo bài đọc từ 21/9/2026](https://www.usagepricing.com/blueprint/activity/xai-2026-09-21-x-search-per-result-billing)
- [copyfomo: tài liệu](https://www.copyfomo.com/docs)
- [DWF Ventures: khoảng 94% ví copy trade trên Fomo lỗ (Yellow)](https://yellow.com/news/fomo-copy-trading-94-percent-wallet-losses)
