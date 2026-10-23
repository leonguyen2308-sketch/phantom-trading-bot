# Copy trade top 1 tới 500 ví cùng lúc (tiền giả)

Đây là bot copy-trade cũ (`./fomo.sh`), nâng lên để theo dõi **cả 500 ví một lúc** thay vì chỉ 1 ví. Mặc định chạy bằng **tiền giả ($100, giá thật)**. Đừng chạy tiền thật khi chưa có kết quả paper đủ lâu (xem phần "Sự thật" cuối bài).

## Bot làm gì

1. Lấy danh sách top ví Solana lãi nhiều nhất 7 ngày (tối đa 500), từ Solana Tracker hoặc từ file ví của bạn.
2. Mở 5 kết nối WebSocket tới Helius, mỗi kết nối theo dõi 100 ví, nên thấy lệnh của cả 500 ví trong khoảng 1 giây.
3. Ví nào **mua** một coin: bot kiểm tra an toàn rồi mua cùng coin đó, bằng 0,25 lần lệnh của họ, **tối thiểu $2 và tối đa $5 mỗi lệnh**.
4. Ví đó **bán**: bot bán cùng tỷ lệ.
5. Một ví mà bot copy **thua 3 lệnh liền** thì bị đuổi 7 ngày. Các ví còn lại vẫn chạy bình thường.
6. Nếu một ví khác mua đúng coin bot đang giữ thì bot không mua thêm (tránh dồn tiền vào một coin).

**Kiểm tra an toàn trước mỗi lệnh:** quyền mint/freeze phải bị thu hồi, thanh khoản từ $15.000, vốn hoá từ $30.000, giá không cao hơn giá ví kia mua quá 20%, phải thử bán ngược được (chống honeypot).

**Giới hạn cứng:** tối đa 15 coin cùng lúc, tối đa 60% vốn trong memecoin, mỗi coin tối đa 6% vốn, lỗ 20% trong ngày thì ngừng mua tới 00:00 UTC, giảm 40% từ đỉnh thì kill switch bán hết, stop loss 50% mỗi coin, giữ quá 24 giờ thì bán.

## Cách chạy trên Mac

Mở Terminal, dán từng khối, chờ xong rồi dán khối tiếp theo.

**1. Tắt bot Desk (để dành hạn mức Helius cho bot này):**

```
launchctl unload ~/Library/LaunchAgents/com.phantom.desk.plist
```

**2. Cập nhật code:**

```
cd ~/phantom-trading-bot
git checkout -- config/desk.yaml
git pull
```

**3. Lấy danh sách ví. Có 2 cách, chọn một:**

*Cách A (tự động, đề nghị):* vào [solanatracker.io](https://www.solanatracker.io), tạo tài khoản, lấy **Data API key** (gói miễn phí). Rồi mở file `.env` và điền vào dòng `SOLANATRACKER_API_KEY=` (dán key ngay sau dấu `=`, không để cách):

```
open -e .env
```

Lưu ý: mình **chưa kiểm chứng được** gói miễn phí của họ có cho dùng bảng xếp hạng này không (máy mình không truy cập được trang của họ). Nếu bước kiểm tra bên dưới báo `Solana Tracker refused`, dùng cách B.

*Cách B (tự lập danh sách):* tạo file `config/wallets.txt`, mỗi dòng một địa chỉ ví Solana, ví tốt nhất ở trên cùng, có thể thêm tên phía sau địa chỉ. Lấy địa chỉ từ bất kỳ bảng xếp hạng ví nào bạn tin (ví dụ kolscan.io, gmgn.ai). Dòng bắt đầu bằng `#` là ghi chú.

```
touch config/wallets.txt
open -e config/wallets.txt
```

**4. Kiểm tra danh sách:**

```
./fomo.sh traders
```

Phải thấy dòng `Top 500 traders (...)` và vài ví đầu tiên. Danh sách đầy đủ lưu ở `reports/fomo_top.txt`. Bot cũng kiểm tra 8 ví đầu xem có thấy lệnh của họ không (dòng `OK`). Chỉ kiểm tra 8 ví vì kiểm tra cả 500 sẽ tốn rất nhiều hạn mức Helius.

**5. Xem thử bằng mô phỏng (không cần key, không tốn gì):**

```
./fomo.sh demo
```

Mở http://localhost:8001 xem dashboard, 40 ví giả đang được copy. Bấm `Ctrl + C` trong Terminal để dừng. Mô phỏng cố ý bất lợi cho người copy (giá bị đẩy trước khi mình mua), nên thường thấy lỗ: số trong demo không nói gì về kết quả thật.

**6. Chạy thật với tiền giả, chạy nền, tự khởi động lại:**

```
scripts/install-fomo-service.sh paper
```

Mở http://localhost:8001. Cắm sạc Mac và **đừng gập màn hình**, Mac ngủ thì bot mù.

## Đọc dashboard

- **Góc trên phải:** `live stream` là WebSocket đang chạy; `polling` là WebSocket rớt (bot chỉ còn đọc được ví đang giữ coin, không bắt được lệnh mua mới).
- **The crowd:** số ví đang copy, số ví bị đuổi, số lệnh bot thấy mỗi giờ, số lệnh đã copy mỗi giờ. Dòng nhỏ phía trên ghi trạng thái kết nối (ví dụ `5/5 websockets up · 500 wallets · reads 311/1000 this hour`).
- **Why trades were not copied:** lý do bỏ qua lệnh, đếm theo ngày. Đa số bị chặn bởi kiểm tra an toàn, đây là việc bình thường.
- **Who is making or losing us money:** ví nào đang làm bot lãi hoặc lỗ nhiều nhất.

## Giới hạn của gói miễn phí (đã đặt sẵn trong `config/fomo.yaml`, mục `watch`)

- **5 kết nối WebSocket x 100 ví = 500 ví** (gói miễn phí của Helius cho 5 kết nối). Mình **chưa kiểm chứng được** Helius có giới hạn số đăng ký trên mỗi kết nối hay không. Nếu họ từ chối, dashboard sẽ ghi `refused by the node`: gửi mình dòng đó.
- **1 triệu credit mỗi tháng.** Mỗi giao dịch bot đọc tốn khoảng 1 credit, nên bot tự giới hạn **1.000 lần đọc mỗi giờ** (khoảng 720.000 mỗi tháng). Hết hạn mức giờ thì bot chỉ đọc lệnh của ví đang giữ coin cho mình (để bán đúng lúc). Chỉ số `reads .../1000` hiện trên dashboard.
- **Ví giao dịch quá 120 lệnh mỗi giờ** là bot hoặc quá nhanh để copy từ Mac: bị bỏ qua 6 giờ.

## Chỉnh số lượng ví và cỡ lệnh (`config/fomo.yaml`)

| Mục | Mặc định | Ý nghĩa |
|---|---|---|
| `lineup_size` | 500 | Copy top bao nhiêu ví (1 tới 500). Muốn chỉ top 50: đặt 50 |
| `follow_all` | true | `false` = cách cũ: chỉ copy 1 ví, thua 2 lệnh liền thì đổi sang ví kế tiếp |
| `max_trade_usd` | 5 | Mỗi lệnh copy tối đa $5 |
| `fire_after_losses` | 3 | Ví thua 3 lệnh liền thì bị đuổi |
| `max_open_positions` | 15 | Tối đa 15 coin cùng lúc |

Sau khi sửa file này, muốn cập nhật code về sau thì chạy `git checkout -- config/fomo.yaml` trước `git pull`, rồi chạy lại `scripts/install-fomo-service.sh paper`.

## Sự thật cần biết

- Theo phân tích của DWF Ventures, khoảng **94% ví copy trade trên FOMO lỗ** sau 3 tháng. Lệnh copy **luôn đến sau** ví gốc vài giây, và trong vài giây đó giá thường đã bị đẩy lên.
- Bảng xếp hạng 7 ngày chủ yếu là ví **may mắn** (đánh nhiều coin, vài coin x100), không có gì đảm bảo tuần sau họ còn lãi. Copy 500 ví làm bot vào lệnh nhiều hơn, nhưng **không làm nó chắc lãi hơn**.
- Vì vậy bot chặn tiền thật bằng `./fomo.sh golive-check` (cần paper đủ 24 giờ và ít nhất 5 lệnh). Hãy để paper chạy **vài ngày** và xem tổng lãi lỗ thật trước khi nghĩ tới tiền thật. Nếu paper lỗ thì đừng chạy live.
- Dashboard và nhật ký không bao giờ hiện khoá API của bạn, nhưng khi chụp màn hình hãy đóng file `.env` trước.
