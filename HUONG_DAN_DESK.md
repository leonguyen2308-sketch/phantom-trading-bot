# Hướng dẫn từ A đến Z: chạy Memecoin Desk (bot memecoin Solana, não Claude) trên MacBook

## Bot này làm gì

Ghép từ 3 video bạn gửi, nhưng sửa những chỗ nguy hiểm:

1. **Lọc bằng code (cái phễu của video 2).** Mỗi phút bot quét vài trăm coin mới trên Jupiter và DexScreener rồi loại dần:
   - **G1, số liệu thị trường:** tuổi pool, thanh khoản, vốn hoá, số lệnh, mua/bán, volume so với vốn hoá, chỉ số "organic".
   - **G2, on-chain:** dev còn quyền mint hay freeze không, top ví giữ bao nhiêu, có nhiều ví mới tạo không, điểm RugCheck.
   - **G3, độ sâu:** báo giá thật cho lệnh của bạn, và thử bán ngược lại xem lỗ bao nhiêu (bắt honeypot và thuế ẩn).
   - **G4, thời điểm:** coin đã rơi xa đỉnh, hoặc vừa tăng dựng đứng thì bỏ.
2. **Hội đồng Claude (video 1).** Coin nào qua hết các cửa thì được gửi cho hội đồng:
   - **ANALYST** đánh giá kiểu sóng.
   - **SKEPTIC** tìm lý do không nên mua.
   - Claude chỉ thấy **con số**, không thấy tên coin hay mô tả, nên kẻ xấu không thể "dụ" AI bằng chữ.
   - Claude chỉ được **phủ quyết hoặc giảm size**, không bao giờ được tăng size.
3. **CHIEF là code.** Quyết định cuối cùng và quản lý rủi ro do code làm, không do AI.
4. **Kế hoạch thoát lệnh viết trước khi mua (video 3, TP-first).** Gồm:
   - stop loss theo độ biến động,
   - chốt lời 1/3 ở +50% và 1/3 ở +100%,
   - sau lần chốt đầu, phần còn lại có trailing stop (không bao giờ lỗ),
   - bán hết ở +300%,
   - thoát nếu volume chết hoặc giữ quá lâu.
5. **Sổ "shadow".** Mọi coin qua code đều được mua giả bằng luật thuần (không Claude). Dashboard so **Claude vs luật thuần**: nếu Claude không thắng thì nó không đáng tiền API.

**Thời gian:**

| Ngày | Việc |
|---|---|
| Ngày 1 | Khoảng 1 giờ cài đặt, rồi chạy tiền giả |
| Tối thiểu 3 ngày sau | Chạy tiền giả **ít nhất 72 giờ và 30 lệnh đã đóng, và phải có lãi**, bot mới cho bật tiền thật |
| Ngày bật live | Khoảng 10 phút |

**Cách làm theo hướng dẫn này:**
- Mỗi ô màu xám là **một** lệnh. Copy, dán vào Terminal (`Cmd + V`), bấm **Enter**.
- Chờ lệnh chạy xong (dòng cuối hiện lại dấu `%`) rồi mới làm bước tiếp.
- Từ bước E trở đi, mỗi lần mở cửa sổ Terminal mới, chạy lệnh này trước:
```bash
cd ~/phantom-trading-bot
```

---

## A. Chuẩn bị

- MacBook **cắm sạc**, Wi-Fi ổn định.
- Điện thoại có **Telegram**.
- Ví **Phantom**.
- Thẻ Visa hoặc Mastercard để nạp tiền API Claude, **tối thiểu $5**.
- Tiền cho bot (chỉ cần khi bật live):
  - **USDC trên mạng Solana**: chỉ số bạn chấp nhận mất hết, ví dụ $100. Với $100, mỗi lệnh là $3 và tối đa 4 coin cùng lúc.
  - Khoảng **0,1 SOL** để trả phí mạng.

## B. Mở Terminal

Bấm `Cmd + Space`, gõ `Terminal`, bấm Enter.

## C. Cài git

Nếu bạn đã cài cho bot FOMO trước đây thì bỏ qua bước này.

```bash
xcode-select --install
```

- Hiện cửa sổ thì bấm **Install** → **Agree**, đợi 5–10 phút.
- Báo `already installed` là đã có, bỏ qua.

## D. Cài uv

Kiểm tra trước. Thấy `uv 0.x.x` là đã có, sang bước E:

```bash
uv --version
```

Nếu báo `command not found`, cài:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Xong thì **đóng hẳn Terminal (`Cmd + Q`)** rồi mở lại.

## E. Tải bot về máy

Dán từng dòng một:

```bash
cd ~
```

```bash
git clone -b claude/determined-goldberg-r7aoiv https://github.com/leonguyen2308-sketch/phantom-trading-bot
```

```bash
cd ~/phantom-trading-bot
```

- Nếu báo `already exists`: bạn chưa xoá bản cũ. Chạy `git pull` thay cho lệnh clone.
- ⚠️ Để bot ở `~/phantom-trading-bot`. **Không chuyển vào Desktop, Documents hay Downloads**, vì macOS sẽ chặn chạy nền.

## F. Chạy demo (không cần key, không tốn tiền)

```bash
./desk.sh demo
```

1. Lần đầu mất 1–2 phút tải thư viện.
2. Thấy dòng `Dashboard: http://localhost:8002` thì mở Safari vào **http://localhost:8002**.
3. Đây là thị trường **giả lập**. Hội đồng Claude cũng là bản giả bằng luật. Bạn sẽ thấy phễu lọc, feed và các lệnh chạy nhanh gấp nhiều lần thời gian thật.
4. Xem xong, về Terminal, giữ `Control` và bấm `C` để dừng.

## G. Lấy RPC miễn phí ở Helius

Đã có từ bot FOMO thì dùng lại.

1. Vào **https://dashboard.helius.dev**, đăng ký, chọn gói **Free**.
2. Mục **RPC / Endpoints**: copy địa chỉ **Mainnet**, dạng `https://mainnet.helius-rpc.com/?api-key=xxxxxxxx`.

## H. Lấy API key miễn phí ở Jupiter

Đã có thì dùng lại. Vào **https://portal.jup.ag** → **API Keys** → tạo key → copy.

## I. Lấy API key Claude (tốn tiền theo lượt dùng)

1. Vào **https://platform.claude.com**, đăng ký và đăng nhập.
2. **Billing** → nạp **$5–10** (Buy credits).
3. Vẫn trong **Billing / Limits**, đặt **giới hạn chi tiêu mỗi tháng**, ví dụ $30. Đây là lớp bảo vệ thứ hai, ngoài giới hạn $3/ngày trong bot.
4. **API Keys** → **Create Key** → đặt tên `memecoin-desk` → copy. Key bắt đầu bằng `sk-ant-` và **chỉ hiện một lần**.

**Chi phí:**
- Mỗi coin được hội đồng xem tốn khoảng **$0,05–0,10** với model mặc định (mạnh nhất).
- Bot dừng gọi Claude khi chi đủ **$3/ngày**, và tối đa 8 lượt mỗi giờ.
- Muốn rẻ hơn: mở `config/desk.yaml`, đổi `model: claude-opus-5-5` thành `claude-sonnet-5-5` hoặc `claude-haiku-5-5`. Nhớ sửa luôn hai dòng giá `price_in_per_mtok` và `price_out_per_mtok` cho đúng model đó, giá xem ở https://platform.claude.com.
- ⚠️ Với tài khoản $100, $3/ngày tiền AI là rất lớn so với lãi có thể có. Bước Q sẽ cho bạn xem Claude có thật sự đáng tiền không.

## J. Tạo bot Telegram

Đã có bot Telegram từ bot FOMO thì dùng lại token và chat id cũ.

1. Telegram → **@BotFather** → Start → gõ `/newbot`.
2. Đặt tên, rồi đặt username **kết thúc bằng `bot`**.
3. Copy **token** dạng `123456789:AAH...`.
4. Bấm vào link bot mới → **Start**.
5. **@userinfobot** → Start → copy **Id** (dãy số). Đó là chat id.

## K. Điền key vào `.env`

Lệnh này tạo file `.env` nếu chưa có:

```bash
./desk.sh --help
```

Mở file:

```bash
open -e .env
```

Điền **ngay sau dấu `=`**:
- không dấu cách,
- không ngoặc kép,
- không xoá dòng khác.

```
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=xxxxxxxx
JUPITER_API_KEY=xxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx
TELEGRAM_BOT_TOKEN=123456789:AAH...
TELEGRAM_CHAT_ID=123456789
```

`Cmd + S` để lưu, đóng TextEdit.

⚠️ Khi chụp màn hình gửi ai (kể cả tôi), **che hết các key**.

## L. Xem bot thấy gì ngoài thị trường thật

Lệnh này chỉ xem: không mua, không gọi Claude. Mất khoảng 1–2 phút.

```bash
./desk.sh scan
```

- Bạn sẽ thấy **cái phễu**: bao nhiêu coin vào, bao nhiêu chết ở mỗi cửa, vì sao chết, và coin nào qua được.
- Không coin nào qua là **bình thường**. Bộ lọc cố ý rất chặt: phần lớn memecoin là rác hoặc lừa đảo.

## M. Thử hội đồng Claude một lần

Lệnh này kiểm tra key và cho biết giá thật của một lượt. Tốn khoảng $0,05–0,10.

```bash
./desk.sh desk-test
```

- Thấy một khối JSON (ý kiến của ANALYST và SKEPTIC) và dòng `Cost of this review: $...` là được.
- Báo `Desk unavailable`: key sai, hoặc chưa nạp tiền ở bước I.

## N. Cài Mac để chạy 24/24

1. **Cắm sạc, không gập máy.**
2. **System Settings → Battery → Options…**:
   - bật **Prevent automatic sleeping on power adapter when the display is off**,
   - bật **Wake for network access**.
3. **Low Power Mode**: chọn **Never**.
4. **General → Software Update** → (i) cạnh Automatic Updates → tắt **Install macOS updates**.

## O. Chạy tiền giả (paper) 24/24 chạy nền

Nếu bot FOMO cũ vẫn còn chạy nền thì tắt nó trước. Báo lỗi `No such file` cũng không sao:

```bash
launchctl unload ~/Library/LaunchAgents/com.phantom.fomo.plist
```

Bật desk ở chế độ paper:

```bash
scripts/install-desk-service.sh paper
```

- Telegram nhắn **"Memecoin Desk started in PAPER mode"**.
- Mở **http://localhost:8002** để xem.
- Bot dùng **$100 tiền giả**, giá thật, có tính phí và trượt giá.
- Đóng Terminal bot vẫn chạy.

## P. Đọc dashboard

| Ô trên dashboard | Ý nghĩa |
|---|---|
| **Balance** | Số dư, lãi/lỗ, đường vốn |
| **The funnel** | Hôm nay bao nhiêu coin vào phễu, chết ở cửa nào, lý do chết nhiều nhất |
| **Analysis · on the table** và **Checks** | Coin gần nhất được xem: số liệu và 6 phép kiểm tra (PASS / FAIL / ?) |
| **The desk** | Ý kiến của ANALYST và SKEPTIC về coin đó |
| Ô **AI $… / $3** ở thanh trên cùng | Tiền Claude đã tiêu hôm nay so với giới hạn |
| **Desk vs rules only** | Lệnh thật (có Claude) so với sổ shadow (luật thuần) |
| **On the ticket · open positions** | Coin đang giữ, kèm stop, mốc chốt lời và trailing |
| **Line feed** | Từng việc của các "ghế": SCOUT (quét), ANALYST, SKEPTIC, CHIEF (quyết định) |

Trên Telegram gõ `/status` để xem nhanh.

## Q. Chờ ít nhất 72 giờ và 30 lệnh

Xem kết quả bất cứ lúc nào:

```bash
./desk.sh stats
```

Đọc kết quả:
- `expectancy_pct`: lãi hoặc lỗ trung bình mỗi lệnh, tính theo % số tiền bỏ vào.
- `ci95_pct`: khoảng tin cậy 95%. Nếu khoảng này chứa số 0, **chưa chứng minh được là có lãi**.
- So dòng `desk (real trades)` với `rules-only (shadow)`:
  - Nếu shadow tốt hơn, Claude **không giúp gì** và bot sẽ không cho bật live. Bạn có hai lựa chọn:
    - Tắt Claude: trong mục `desk:` của `config/desk.yaml`, đặt `enabled: false` và `when_unavailable: rules_only`. Bot sẽ chấm điểm bằng sổ shadow (luật thuần) và không tốn tiền API. Nhớ chạy lại `./desk.sh approve`.
    - Hoặc dừng hẳn.
- Bot **trừ tiền Claude** vào lãi paper khi chấm điểm. Lãi $6 mà tiền Claude $7 thì vẫn là lỗ.
- Nếu sau 72 giờ paper vẫn lỗ: **đừng bật live**. Bot sẽ không cho bật đâu.

## R. Tạo ví riêng cho bot

1. Phantom → bấm avatar góc trên bên trái → **Add / Connect Wallet** → **Create New Account**.
2. Đặt tên **Desk Bot**.
3. **Không bao giờ** dùng ví chính.

## S. Nạp tiền vào ví bot

Copy địa chỉ **Solana** của ví Desk Bot, rồi chọn một trong hai cách:

- **Từ ví chính trong Phantom:** **Send** → **USDC** → dán địa chỉ → gửi. Làm tương tự với **0,1 SOL**.
- **Từ sàn:** Withdraw **USDC**, **Network chọn Solana (SOL)**; làm tương tự với SOL. Chọn sai mạng là mất tiền.

## T. Đưa ví bot vào `.env`

1. Phantom: **Settings** → **Manage Accounts** → **Desk Bot** → **Show Private Key** → mật khẩu → **Solana** → copy.
2. Mở file:
```bash
open -e .env
```
3. Điền hai dòng sau, rồi `Cmd + S`:
```
DESK_BOT_ADDRESS=dia_chi_solana_cua_vi_desk_bot
DESK_BOT_PRIVATE_KEY=private_key_vua_copy
```

⚠️ **Không gửi private key cho ai, kể cả tôi.** Ai có key là rút được hết tiền.

## U. Kiểm tra ví, duyệt luật, đọc rủi ro

Chạy từng lệnh một.

**Kiểm tra ví:** phải thấy đúng số USDC và SOL, và dòng `Nothing was sent`.
```bash
./desk.sh wallet-check
```

**Duyệt luật:** đọc hết, gõ `APPROVE` rồi Enter. Từ giờ, nếu file luật bị đổi lúc đang live, bot sẽ bán hết và dừng.
```bash
./desk.sh approve
```

**Đọc rủi ro:** đọc kỹ từng điều, mỗi điều gõ `I ACCEPT` rồi Enter.
```bash
./desk.sh ack-risks
```

## V. Kiểm tra lần cuối

```bash
./desk.sh golive-check
```

Mọi dòng phải **PASS**, trừ dòng `DESK_LIVE=yes`; dòng đó sẽ PASS sau bước W.
- Dòng `paper long enough` FAIL: chờ thêm.
- Dòng `paper made money` FAIL: **dừng ở đây**. Bot lỗ bằng tiền giả (đã trừ tiền Claude) thì cũng sẽ lỗ bằng tiền thật.
- Dòng `desk beats rules-only` FAIL: Claude làm kém hơn luật thuần. Xem bước Q.

## W. Bật LIVE

1. Mở `.env`, sửa `DESK_LIVE=no` thành **`DESK_LIVE=yes`**, rồi `Cmd + S`:
```bash
open -e .env
```

2. Chạy lệnh này. Nó tắt bản paper và chạy live 24/24:
```bash
scripts/install-desk-service.sh live
```

3. Telegram nhắn **"started in LIVE mode"**, dashboard hiện **LIVE**.

## X. Bot tự bảo vệ thế nào

| Chuyện gì xảy ra | Bot làm gì |
|---|---|
| Mỗi lệnh | Chỉ 3% số dư, tối thiểu $3, tối đa $20. Tối đa 4 coin, tổng không quá 25% số dư |
| Lỗ 6% trong ngày | Ngừng mua đến 7 giờ sáng hôm sau, giờ Việt Nam (00:00 UTC). Vẫn quản lý coin đang giữ |
| Thua 5 lệnh liên tiếp | Ngừng mua 12 giờ |
| Số dư giảm 25% so với đỉnh | **Kill switch**: bán hết, dừng hẳn |
| Mất mạng, hoặc không đọc được giá hay ví khoảng 2–3 phút | **Kill switch** |
| Jupiter, DexScreener hoặc RPC lỗi liên tục (chỉ phần quét coin mới) | Ngừng mua và báo. Tự mua lại khi hết lỗi. Coin đang giữ vẫn được quản lý, không bán hàng loạt |
| Không đọc được RugCheck hoặc danh sách ví lớn | Không mua coin đó, xem lại sau 30 phút. Không biết thì không mua |
| Coin mất giá trên Jupiter | Lấy giá từ DexScreener, rồi từ báo giá bán. Không nguồn nào có giá trong 2 phút: coi như mất trắng, bán ngay nếu còn bán được |
| Một coin bán mãi không được | Thử lại mỗi 10 giây, sau 5 phút thì mỗi phút. Coin dưới $5 thì bỏ (ghi lỗ). Không làm bật kill switch |
| Hết $3 tiền Claude trong ngày | Không mua thêm, chờ sang ngày mới |
| Giá chạy quá 8% trong lúc hội đồng đang nghĩ | Bỏ lệnh, không đuổi giá |

## Y. Theo dõi hằng ngày

| Việc | Cách làm |
|---|---|
| Xem nhanh | Telegram `/status` |
| Xem chi tiết | **http://localhost:8002** |
| **Dừng khẩn cấp và bán hết** | Telegram `/kill`, nút KILL SWITCH trên dashboard, hoặc `./desk.sh kill` (lệnh này dừng cả paper lẫn live) |
| Mỗi tuần | `./desk.sh stats --mode live`: Claude có còn thắng luật thuần không? |
| Mỗi tuần | Số SOL trong ví bot dưới 0,05 thì nạp thêm |
| Mỗi tuần | Xem tiền Claude đã tiêu ở https://platform.claude.com |

## Z. Sự cố, cập nhật, rút tiền

**Bật lại sau kill switch.** Gõ `RESET` cho lệnh đầu, rồi chạy lệnh thứ hai:
```bash
./desk.sh reset
```

```bash
scripts/install-desk-service.sh live
```

**Tắt hẳn bot:** bot sẽ ngừng mua ngay, chờ các lệnh đang gửi dở xong (tối đa 2 phút), rồi mới tắt.
```bash
launchctl unload ~/Library/LaunchAgents/com.phantom.desk.plist
```

**Cập nhật code mới.** Luôn tắt bot trước. Chạy từng lệnh; đến `approve` thì gõ `APPROVE`:
```bash
launchctl unload ~/Library/LaunchAgents/com.phantom.desk.plist
```

```bash
git pull
```

```bash
./desk.sh approve
```

```bash
scripts/install-desk-service.sh live
```

**Rút tiền:**
1. Tắt bot.
2. Rút trong Phantom.
3. `./desk.sh reset` (gõ `RESET`), để bot không hiểu khoản rút là lỗ.
4. Bật lại.

**Lỗi thường gặp:**

| Thấy dòng này | Làm gì |
|---|---|
| `command not found: uv` | Đóng hẳn Terminal (`Cmd + Q`), mở lại |
| `no such file or directory: ./desk.sh` | Chạy `cd ~/phantom-trading-bot` |
| `permission denied: ./desk.sh` | Chạy `chmod +x desk.sh scripts/*.sh` |
| `Missing in .env: ...` | Thiếu key ở bước K hoặc T |
| `Desk unavailable` | Sai `ANTHROPIC_API_KEY`, hết credit, hoặc Claude đang quá tải. Không có Claude thì bot **không mua** |
| `is not approved` | Chạy `./desk.sh approve` |
| `NOT READY` | Đọc dòng FAIL trong `./desk.sh golive-check` |
| Banner vàng `No new buys: jupiter keeps failing` | Jupiter trả lỗi liên tục. Thường do key Jupiter sai hoặc đã bị xoá. Kiểm tra bằng lệnh `curl` ở mục "Jupiter lỗi" bên dưới |
| Dashboard ghi `no price` | Không nguồn nào báo giá coin đó. Sau 2 phút bot coi nó là mất trắng và bán nếu được |
| Phễu chết hết ở `onchain` với lý do `RugCheck unavailable` | RugCheck đang lỗi. Bot không mua cho đến khi RugCheck chạy lại. Đó là cố ý |
| Dashboard không mở | `launchctl list \| grep desk`; không thấy dòng nào thì chạy lại bước O hoặc W. Xem lỗi: `tail -50 logs/desk-service.err` |

---

**Nhớ:**
- Khoảng **74% người mua memecoin Solana bị lỗ**, và 1% người mua lấy hơn nửa số lãi (Bitquery, 2,9 triệu ví).
- Các video bạn gửi là quảng cáo tự báo cáo. Một video còn gắn sẵn link giới thiệu của người làm video vào lệnh mua.
- Chưa ai chứng minh AI giúp thắng memecoin; bot này được làm để **đo** điều đó bằng tiền giả trước.
- Ở Việt Nam, giao dịch crypto trên nền tảng không phép vẫn là vùng xám pháp lý.
- Chỉ để trong ví bot số tiền bạn chấp nhận mất hết.

**Jupiter lỗi (kiểm tra key, chỉ in mã trạng thái, không lộ key):**
```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "x-api-key: $(grep '^JUPITER_API_KEY=' .env | cut -d= -f2-)" "https://api.jup.ag/tokens/v2/toptrending/5m?limit=1"
```
- `200`: key đúng, Jupiter chỉ lỗi tạm.
- `401` hoặc `403`: key sai hoặc đã bị xoá. Tạo Server key mới ở https://portal.jup.ag, dán vào `.env`, rồi chạy lại `scripts/install-desk-service.sh paper`.
- `429`: gọi quá nhiều lần, chờ vài phút.
