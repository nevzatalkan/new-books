# 📚 Goodreads Haftalık Digest

Her Pazartesi sabahı Goodreads RSS feed'inizden son 7 günde okunan kitapları
toplayıp kategorilere göre gruplandırarak HTML e-posta olarak gönderir.

GitHub Actions ile tamamen otomatik çalışır — hiçbir sunucu gerekmez.

---

## Nasıl Çalışır?

```
Goodreads RSS Feed
       │
       ▼
  src/digest.py          ← kategorilere göre gruplar
       │
       ├── categories.yml ← aktif / pasif kategori tanımları
       │
       ▼
  Gmail SMTP → 📧 Haftalık HTML e-posta
```

GitHub Actions her **Pazartesi 08:00 UTC**'de workflow'u tetikler.

---

## Kurulum

### 1. Goodreads RSS URL'sini Bul

1. [goodreads.com](https://www.goodreads.com) profilinize gidin.
2. Sayfanın alt kısmındaki **RSS** bağlantısına sağ tıklayın → URL'yi kopyalayın.
3. URL şu formatta olacaktır:
   ```
   https://www.goodreads.com/user/updates_rss/12345678
   ```

### 2. Gmail App Password Oluştur

Google hesabınızda **2 Adımlı Doğrulama** aktif olmalıdır.

1. [myaccount.google.com/security](https://myaccount.google.com/security) adresine gidin.
2. **Güvenlik → 2 Adımlı Doğrulama → Uygulama şifreleri** yolunu izleyin.
3. Yeni bir şifre oluşturun (örn. "Goodreads Digest") — 16 karakterlik kodu kopyalayın.

### 3. GitHub Secrets Ekle

Repo'nuzda **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Adı           | Değer                                              |
|----------------------|----------------------------------------------------|
| `GOODREADS_RSS_URL`  | `https://www.goodreads.com/user/updates_rss/12345` |
| `GMAIL_USER`         | `you@gmail.com`                                    |
| `GMAIL_APP_PASSWORD` | `xxxxxxxxxxxxxxxxxxxx` (16 karakter, boşluksuz)    |
| `TO_EMAIL`           | `recipient@example.com`                            |

> Birden fazla alıcı için `TO_EMAIL` değerini virgülle ayırın:
> `ali@example.com,ayse@example.com`

### 4. Kategorileri Yapılandır

`categories.yml` dosyasını düzenleyerek aktif / pasif kategorileri ayarlayın:

```yaml
categories:
  - name: "Yapay Zeka & Robot Teknolojileri"
    shelves: [ai, machine-learning, robotics]
    active: true   # ← bu kategori e-postaya dahil edilir

  - name: "Kurgu & Edebiyat"
    shelves: [fiction, novel]
    active: false  # ← şimdilik devre dışı
```

`shelves` listesi, Goodreads raf etiketleriyle eşleştirilir. Bir kitap birden
fazla kategoriye girebilir. Eşleşmeyen kitaplar "Diğer" bölümüne düşer.

---

## Yerel Test

```bash
# Bağımlılıkları kur
pip install -r requirements.txt

# Ortam değişkenlerini ayarla
cp .env.example .env
# .env dosyasını düzenle ve değerleri doldur

# Ortam değişkenlerini yükle ve scripti çalıştır
export $(grep -v '^#' .env | xargs)
python src/digest.py
```

---

## Manuel GitHub Actions Tetikleme

**Actions → Goodreads Haftalık Özet → Run workflow** ile dilediğiniz zaman
manuel olarak çalıştırabilirsiniz. Secrets'lar doğru ayarlanmışsa e-posta hemen gönderilir.

---

## Dosya Yapısı

```
new-books/
├── .github/
│   └── workflows/
│       └── weekly-digest.yml   # Cron: Her Pazartesi 08:00 UTC
├── src/
│   └── digest.py               # Ana Python scripti
├── categories.yml              # Kategori konfigürasyonu
├── requirements.txt
├── .env.example                # Ortam değişkenleri şablonu
└── README.md
```
