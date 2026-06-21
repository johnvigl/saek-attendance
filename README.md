# ΣΑΕΚ - Σύστημα Απουσιολογίου & Βιβλίου Ύλης

Ένα ολοκληρωμένο σύστημα καταγραφής διδασκαλίας, απουσιών και διαχείρισης για τις Σχολές Ανώτερης Επαγγελματικής Κατάρτισης (ΣΑΕΚ).

## Περιγραφή

Το σύστημα επιτρέπει:

- **Εκπαιδευτές**: Καταγραφή ημερήσιας ύλης και απουσιών για κάθε μάθημα.
- **Φοιτητές**: Πρόσβαση στο προσωπικό τους ιστορικό απουσιών.
- **Γραμματεία/Admin**: Πλήρης διαχείριση (μαθήματα, εκπαιδευτές, φοιτητές, αναθέσεις, εξάμηνα) και στατιστικά απουσιών.
- **Εκτυπώσεις**: Αναφορές απουσιολογίου και βιβλίου ύλης.

## Απαιτήσεις

- **Docker** (έκδοση 20.10+)
- **Docker Compose** (έκδοση 2.0+)
- Λειτουργικό σύστημα: Linux, macOS

## Εγκατάσταση

1. Κατεβάστε τον κώδικα

`git clone https://github.com/johnvigl/saek-attendance.git`

2. Εκτελέστε

`cd saek-attendance` (είσοδος στο φάκελο)

Αντιγράψτε το `.env.example` σε `.env` και συμπληρώστε τα στοιχεία σας.

`cp .env.example .env` (δημιουργία .env από το πρότυπο)

`nano .env` (Επεξεργασία του αρχείου .env)

- αλλάξτε τα credentials (χρησιμοποιήστε ισχυρά passwords)
- ορίστε τα domains (ακολουθεί παράδειγμα):

Yποστηρίζονται 2 ξεχωριστά subdomains για τη διαχείριση και τους εκπαιδευτές/καταρτιζόμενους.
```python
FRONTEND_DOMAIN=apousies.saek_example.gr
ADMIN_DOMAIN=grammateia.saek_example.gr
```
- **Αν δεν έχετε external network** (π.χ. Caddy), ανοίξτε το `docker-compose.yml` και σχολιάστε:
   - Τη γραμμή `- external_network` στο service `app`.
   - Ολόκληρο το μπλοκ `external_network` στο τέλος του αρχείου.

3. Εκκίνηση της εφαρμογής

`docker-compose up -d`

4. Πρόσβαση

Εκπαιδευτές/Φοιτητές: http://localhost:5411 ή apousies.saek_example.gr

Γραμματεία/Admin: http://localhost:5411 ή grammateia.saek_example.gr

6. Αρχική σύνδεση
Admin username: `admin`, password `admin` (αλλάξτε τα αμέσως)
   
Εκπαιδευτές/Καταρτιζόμενοι: Σύνδεση με email + OTP (απαιτεί ρύθμιση λογαριασμού email αποστολής)

## Ενημερώσεις
- Σε περίπτωση σημαντικής ενημέρωσης θα λαμβάνετε ειδοποίηση εντός της εφαρμογής.
- Για να ενημερώσετε την εφαρμογή στην τελευταία έκδοση, εκτελέστε:

`chmod +x update.sh`


`./update.sh`

## Ρύθμιση Email (για OTP και αποστολή μαζικών email)

- Συνδεθείτε ως admin.
- Πηγαίνετε στις Ρυθμίσεις → Email.
- Προσθέστε έναν λογαριασμό αποστολέα (π.χ. Gmail με App Password).
- Ενεργοποιήστε τον λογαριασμό (checkbox "Ενεργός").
- Ορίστε τον "Προς" και τους "CC" παραλήπτες (προαιρετικά).
- Οι παραλήπτες από την εφαρμογή (εκπαιδευτές και καταρτιζόμενοι) είναι πάντοτε με κρυφή κοινοποίηση (BCC)

## Εισαγωγή δεδομένων (CSV)

Ως admin, μπορείτε να εισάγετε: `http://localhost:5411/docs`

- Εκπαιδευτές: `surname, name, mail, phone`

- Μαθήματα: `specialty_name, semester, department, team, lesson_name, type_indicator, classroom, weekly_hours, surname, name`

- Φοιτητές: `amk, surname, name, father_name, mother_name, mail, phone, specialty_name, semester, department, team`

Τα CSV αρχεία πρέπει να είναι σε UTF-8 με κόμμα (,) ως διαχωριστικό.

## Δομή φακέλων

```plaintext
saek-attendance/
├── app/
│   ├── static/           # στατικά αρχεία (logo, css, js)
│   ├── main.py           # κύρια εφαρμογή FastAPI
│   ├── *.html            # σελίδες frontend
│   ├── requirements.txt  # Python dependencies
│   └── Dockerfile        # build αρχείο
├── docker-compose.yml
├── .env.example          # παράδειγμα μεταβλητών
├── .env                  # πραγματικές μεταβλητές (δημιουργείται από τον χρήστη)
├── README.md
└── LICENSE               # GNU GPL v3
```

## Συχνά προβλήματα

- "Database not available"
    Βεβαιωθείτε ότι το container saek_db τρέχει, εκτελέστε `docker ps`
    Ελέγξτε τα credentials στο .env.

- "No active semester configured"
    Συνδεθείτε ως admin → πηγαίνετε στις Ρυθμίσεις → Εξάμηνα → δημιουργήστε ένα εξάμηνο και ορίστε το ως ενεργό.

- Δεν στέλνονται OTP emails
    Ελέγξτε ότι έχετε προσθέσει τουλάχιστον έναν ενεργό λογαριασμό αποστολέα.
    Βεβαιωθείτε ότι το SMTP host/port/username/password είναι σωστά (για Gmail χρειάζεται App Password).

## Άδεια χρήσης

Το έργο διανέμεται υπό την GNU General Public License v3.0 ή νεότερη.
Δείτε το αρχείο LICENSE για λεπτομέρειες.

## Συνεισφορά

Αν θέλετε να βελτιώσετε το σύστημα, κάντε fork, εφαρμόστε αλλαγές και αποστείλετε pull request.
