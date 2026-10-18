# Hướng dẫn từ A đến Z: chạy FOMO Copy Bot live 24/24 trên MacBook

**Tổng thời gian:**
- **Ngày 1:** khoảng 45 phút làm, rồi để bot chạy thử bằng tiền giả 24 giờ (bắt buộc trước khi chạy tiền thật).
- **Ngày 2:** khoảng 5 phút để bật live.

**Cách làm theo hướng dẫn này:**
- Mỗi ô màu xám là lệnh. Bấm nút copy ở góc ô (hoặc bôi đen rồi `Cmd + C`), dán vào Terminal (`Cmd + V`), bấm **Enter**.
- Chờ lệnh chạy xong, tức là dòng cuối hiện lại dấu `%`, rồi mới làm bước tiếp theo.
- Từ bước E trở đi, mỗi lần mở cửa sổ Terminal mới, chạy lệnh này trước tiên để vào thư mục bot:
```bash
cd ~/phantom-trading-bot
```

---

## A. Chuẩn bị những gì

- MacBook, **cắm sạc**, Wi-Fi ổn định.
- Điện thoại có app **Telegram** (để bot báo tin và để bạn dừng bot từ xa).
- Ví **Phantom** (bạn đã có).
- Tiền cho bot:
  - **USDC trên mạng Solana**: chỉ số bạn chấp nhận mất hết, ví dụ $50. Với $50, mỗi lệnh copy tối đa $10.
  - Khoảng **0,1 SOL** để trả phí mạng.

## B. Mở Terminal

Bấm `Cmd + Space`, gõ `Terminal`, bấm Enter. Một cửa sổ chữ hiện ra; mọi lệnh bên dưới đều dán vào đây.

## C. Cài git (công cụ của Apple)

```bash
xcode-select --install
```

- Hiện cửa sổ thì bấm **Install** → **Agree**, đợi 5–10 phút.
- Nếu báo `already installed` thì bỏ qua.

Kiểm tra (thấy `git version ...` là được):

```bash
git --version
```

## D. Cài uv (tự cài Python và mọi thứ bot cần)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Xong thì **đóng hẳn Terminal bằng `Cmd + Q`** rồi mở lại (bước B). Kiểm tra (thấy `uv 0.x.x` là được):

```bash
uv --version
```

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

⚠️ Bot phải nằm ở `~/phantom-trading-bot` như trên. **Đừng chuyển vào Desktop, Documents hay Downloads**, vì macOS sẽ chặn bot chạy nền.

## F. Chạy thử demo (chưa cần key)

```bash
./fomo.sh demo
```

1. Lần đầu mất 1–2 phút để tải thư viện.
2. Khi thấy dòng `Dashboard: http://localhost:8001`, mở Safari vào **http://localhost:8001**. Đây là dữ liệu **giả lập**.
3. Xem xong, quay lại Terminal, giữ phím `Control` và bấm `C` để dừng.

## G. Lấy RPC miễn phí ở Helius (để bot theo dõi ví trader)

1. Vào **https://dashboard.helius.dev**, đăng ký (Google hoặc email), chọn gói **Free**.
2. Trong dashboard, tìm mục **RPC / Endpoints**, copy địa chỉ **Mainnet**. Nó có dạng `https://mainnet.helius-rpc.com/?api-key=xxxxxxxx`.

## H. Lấy API key miễn phí ở Jupiter (để báo giá và mua bán)

Vào **https://portal.jup.ag**, đăng nhập, vào mục **API Keys**, tạo key mới và copy.

## I. Tạo bot Telegram (để nhận thông báo trên điện thoại)

1. Trong Telegram, tìm **@BotFather** → bấm Start → gõ `/newbot`.
2. Đặt tên (ví dụ `My Fomo Bot`), rồi đặt username **kết thúc bằng `bot`** (ví dụ `fomo_cua_toi_bot`).
3. BotFather gửi lại một **token** dạng `123456789:AAH...`. Copy nó.
4. Bấm vào link bot vừa tạo và bấm **Start**. Nếu không bấm Start, bot sẽ không nhắn được cho bạn.
5. Tìm **@userinfobot** → Start. Nó trả về **Id** của bạn (một dãy số). Đó là chat id.

## J. Điền key vào file `.env`

```bash
open -e .env
```

File mở trong TextEdit. Tìm từng dòng dưới đây và dán giá trị **ngay sau dấu `=`**:
- không có dấu cách,
- không có dấu ngoặc kép,
- không xoá các dòng khác.

```
SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=xxxxxxxx
JUPITER_API_KEY=xxxxxxxx
TELEGRAM_BOT_TOKEN=123456789:AAH...
TELEGRAM_CHAT_ID=123456789
```

Bấm `Cmd + S` để lưu, rồi đóng TextEdit.

## K. Chọn trader: top tuần trên FOMO

Mở app FOMO → **Leaderboard** → chọn tab **tuần (7D)**. Ghi lại **6 username đầu tiên** theo đúng thứ tự.

## L. Tìm ví Solana của từng trader

App FOMO ẩn địa chỉ ví, nên phải dùng công cụ tra cứu:

1. Mở **https://fomowalletfinder.com** hoặc **https://fomolens.app**.
2. Gõ username của trader.
3. Copy **địa chỉ Solana**: dài 32–44 ký tự, gồm chữ và số, **không** bắt đầu bằng `0x`.

Các công cụ này không chính thức, nên bước N sẽ kiểm tra lại.

## M. Điền trader vào file

```bash
open -e config/traders.yaml
```

Kéo xuống cuối file, **xoá dòng `traders: []`**, dán đoạn dưới đây vào, rồi thay tên và ví thật:

```yaml
traders:
  - handle: ten_trader_1
    wallet: DIA_CHI_VI_SOLANA_1
  - handle: ten_trader_2
    wallet: DIA_CHI_VI_SOLANA_2
  - handle: ten_trader_3
    wallet: DIA_CHI_VI_SOLANA_3
```

Quy tắc:
- Đúng **2 dấu cách** trước `- handle` và **4 dấu cách** trước `wallet`. Dùng phím cách, không dùng Tab.
- Tên trader không có `@`.
- Người hạng 1 ghi đầu tiên.

Bấm `Cmd + S` để lưu.

## N. Kiểm tra bot có thấy lệnh của trader không

```bash
./fomo.sh traders
```

| Kết quả | Ý nghĩa và cách xử lý |
|---|---|
| `... trades found ... OK` | Tốt |
| `NO trades found` | Ví sai, hoặc người đó không trade trên Solana. Xoá và thay người khác. |
| `is not a Solana wallet address` | Gõ sai địa chỉ. Sửa lại ở bước M. |

## O. (Nên làm) Xem copy họ 7 ngày qua thì lời hay lỗ

```bash
./fomo.sh research
```

Mất vài phút. Nhìn bảng cuối, ở các dòng **3%** và **8%**: nếu cột P&L âm thì copy nhóm này sẽ lỗ, nên đổi trader ở bước K–N.

## P. Cài Mac để chạy 24/24

1. **Cắm sạc liên tục, không gập máy.** Gập máy là Mac ngủ và bot dừng.
2. Vào **System Settings → Battery → Options…**:
   - bật **Prevent automatic sleeping on power adapter when the display is off**,
   - bật **Wake for network access**.
3. **System Settings → Battery → Low Power Mode**: chọn **Never**.
4. **System Settings → General → Software Update** → bấm (i) cạnh Automatic Updates → tắt **Install macOS updates**. Nếu không, máy có thể tự khởi động lại lúc nửa đêm.

Màn hình tự tắt thì không sao, bot vẫn chạy.

## Q. Bật chạy thử bằng tiền giả (paper), chạy nền

```bash
scripts/install-fomo-service.sh paper
```

- Telegram sẽ nhắn **"FOMO copy bot started in PAPER mode"**. Mở **http://localhost:8001** để xem.
- **Ghi lại giờ bắt đầu**: sau 24 giờ mới bật live được.
- Bot tự chạy kể cả khi bạn đóng Terminal.

## R. Tạo ví riêng cho bot trong Phantom

1. Mở Phantom → bấm vào tên ví hoặc avatar ở góc trên bên trái → **Add / Connect Wallet** → **Create New Account**.
2. Đặt tên **FOMO Bot**.
3. **Không bao giờ** dùng ví chính cho bot.

## S. Nạp tiền vào ví bot

Lấy địa chỉ ví bot: trong Phantom, chọn ví **FOMO Bot** → bấm vào địa chỉ → chọn địa chỉ **Solana** để copy.

**Cách 1: chuyển từ ví chính trong Phantom**
1. Chuyển sang ví chính → **Send** → **USDC** → dán địa chỉ ví bot → nhập số tiền → gửi.
2. Làm tương tự với **0,1 SOL**.

**Cách 2: rút từ sàn (ví dụ Binance)**
1. Withdraw **USDC** → dán địa chỉ ví bot → **Network chọn Solana (SOL)**. Chọn sai mạng là mất tiền.
2. Làm tương tự với **0,1 SOL**, cũng chọn mạng Solana.

## T. Đưa ví bot vào file `.env`

1. Trong Phantom: **Settings** → **Manage Accounts** → chọn **FOMO Bot** → **Show Private Key** → nhập mật khẩu → chọn **Solana** → copy.
2. Mở file:
```bash
open -e .env
```
3. Điền hai dòng sau, rồi `Cmd + S`:
```
FOMO_BOT_ADDRESS=dia_chi_solana_cua_vi_bot
FOMO_BOT_PRIVATE_KEY=private_key_vua_copy
```

⚠️ **Không gửi private key cho bất kỳ ai, kể cả tôi.** Ai có key này đều rút được hết tiền trong ví bot.

## U. Kiểm tra ví, duyệt cấu hình, đọc rủi ro

Chạy từng lệnh một.

**Kiểm tra ví:** phải thấy đúng số USDC và SOL vừa nạp, và dòng `Nothing was sent`.
```bash
./fomo.sh wallet-check
```

**Duyệt cấu hình:** gõ `APPROVE` (chữ in hoa) rồi Enter.
```bash
./fomo.sh approve
```

**Đọc rủi ro:** đọc từng rủi ro, mỗi cái gõ `I ACCEPT` (chữ in hoa) rồi Enter.
```bash
./fomo.sh ack-risks
```

## V. Chờ 24 giờ

Trong lúc chờ, theo dõi trên Telegram (gõ `/status` cho bot của bạn) hoặc trên **http://localhost:8001**. Copy feed sẽ hiện các dòng:

| Dòng | Ý nghĩa |
|---|---|
| `[COPY]` | Bot vừa copy lệnh mua |
| `[EXIT]` | Bot vừa bán |
| `[SKIP]` | Bot bỏ qua coin không an toàn |
| `[FIRE]` | Bot vừa đuổi một trader |

Nếu vài giờ liền không có dòng nào: chạy lại `./fomo.sh traders`. Có thể các trader đang nghỉ, hãy chọn người hoạt động nhiều hơn.

## W. Kiểm tra lần cuối (sau 24 giờ)

```bash
./fomo.sh golive-check
```

Mọi dòng phải là **PASS**, trừ dòng `FOMO_LIVE=yes`; dòng đó sẽ PASS sau bước X. Nếu dòng `paper run works` báo chưa đủ **5 copies**, chờ thêm cho đủ.

## X. Bật LIVE

1. Mở file và sửa `FOMO_LIVE=no` thành **`FOMO_LIVE=yes`**, rồi `Cmd + S`:
```bash
open -e .env
```

2. Chạy lệnh này. Nó tự tắt bản paper và chạy bản live 24/24:
```bash
scripts/install-fomo-service.sh live
```

3. Telegram nhắn **"started in LIVE mode"**, dashboard hiện chữ **LIVE**. Bot đang trade bằng tiền thật. 🎯

## Y. Theo dõi hằng ngày

| Việc | Cách làm |
|---|---|
| Xem nhanh trên điện thoại | Telegram: `/status` |
| Xem chi tiết trên Mac | **http://localhost:8001** |
| **Dừng khẩn cấp và bán hết** | Telegram: `/kill`, hoặc nút KILL SWITCH trên dashboard, hoặc `./fomo.sh kill` |
| Mỗi tuần | Cập nhật top trader (bước K–N), rồi chạy lại `scripts/install-fomo-service.sh live` |
| Mỗi tuần | Xem số SOL trong ví bot. Dưới 0,05 thì nạp thêm. |

## Z. Sự cố, cập nhật, rút tiền

**Bot tự dừng (kill switch)** khi số dư giảm 40% so với đỉnh, mất mạng quá khoảng 2,5 phút, hoặc bạn bấm kill. Nó bán hết rồi đứng yên. Muốn chạy lại, gõ `RESET` cho lệnh đầu, rồi chạy lệnh thứ hai:

```bash
./fomo.sh reset
```

```bash
scripts/install-fomo-service.sh live
```

**Tắt hẳn bot:**

```bash
launchctl unload ~/Library/LaunchAgents/com.phantom.fomo.plist
```

**Sửa `.env` hoặc `config/traders.yaml`:** sửa xong thì chạy lại `scripts/install-fomo-service.sh live` để bot đọc thay đổi.

**Cập nhật code mới:** luôn tắt bot trước. Nếu `config/fomo.yaml` bị đổi trong lúc đang live, bot sẽ bán hết và dừng. Chạy từng lệnh, đến `approve` thì gõ `APPROVE`:

```bash
launchctl unload ~/Library/LaunchAgents/com.phantom.fomo.plist
```

```bash
git pull
```

```bash
./fomo.sh approve
```

```bash
scripts/install-fomo-service.sh live
```

**Rút tiền khỏi ví bot:**
1. Tắt bot bằng lệnh `launchctl unload ...` ở trên.
2. Rút tiền trong Phantom.
3. Chạy `./fomo.sh reset` (gõ `RESET`). Lệnh này đặt lại mức đỉnh, để bot không hiểu khoản rút là lỗ.
4. Bật lại bằng `scripts/install-fomo-service.sh live`.

**Lỗi thường gặp:**

| Thấy dòng này | Làm gì |
|---|---|
| `command not found: uv` | Đóng hẳn Terminal (`Cmd + Q`), mở lại |
| `no such file or directory: ./fomo.sh` | Chưa vào thư mục bot: chạy `cd ~/phantom-trading-bot` |
| `permission denied: ./fomo.sh` | Chạy `chmod +x fomo.sh scripts/*.sh` |
| `xcrun: error` | Làm lại bước C |
| `Missing in .env: ...` | Thiếu key ở bước J hoặc T |
| `No traders` | Chưa làm bước M |
| `is not approved` | Chạy `./fomo.sh approve` |
| Telegram im lặng | Chưa bấm Start với bot (bước I), hoặc sai chat id. Sửa xong chạy lại `scripts/install-fomo-service.sh live` |
| Dashboard không mở được | Chạy `launchctl list \| grep fomo`. Không thấy dòng nào là bot đang tắt: chạy lại `scripts/install-fomo-service.sh live`. Xem lỗi bằng `tail -50 logs/fomo-service.err` |

---

**Nhớ:** khoảng 94% ví copy trade trên FOMO lỗ (DWF Ventures). Video $42 → $5.739 là quảng cáo. Chỉ để trong ví bot số tiền bạn chấp nhận mất hết.
