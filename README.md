# ΣΑΕΚ - Σύστημα Απουσιολογίου & Βιβλίου Ύλης

Ένα ολοκληρωμένο σύστημα καταγραφής διδασκαλίας, απουσιών και διαχείρισης ύλης για τις Σχολές Ανώτερης Επαγγελματικής Κατάρτισης (ΣΑΕΚ).

## Περιγραφή

Το σύστημα επιτρέπει:

- **Εκπαιδευτές**: Καταγραφή ημερήσιας ύλης και απουσιών για κάθε μάθημα.
- **Φοιτητές**: Πρόσβαση στο προσωπικό τους ιστορικό απουσιών.
- **Γραμματεία/Admin**: Πλήρης διαχείριση (μαθήματα, εκπαιδευτές, φοιτητές, αναθέσεις, εξάμηνα) και στατιστικά απουσιών.
- **Εκτυπώσεις**: Αναφορές απουσιολογίου και βιβλίου ύλης.

## Απαιτήσεις

- **Docker** (έκδοση 20.10+)
- **Docker Compose** (έκδοση 2.0+)
- Λειτουργικό σύστημα: Linux, macOS, Windows (με WSL2)

## Εγκατάσταση

1. Κατεβάστε τον κώδικα
git clone https://github.com/johnvigl/saek-attendance.git
cd saek-attendance

2. Δημιουργήστε το αρχείο .env
'''cp .env.example .env'''
Επεξεργασία .env   
  ### α. αλλάξτε τα credentials (χρησιμοποιήστε ισχυρά passwords)
  ### β. ορίστε τα domains (για παράδειγμα):
            Yποστηρίζονται 2 ξεχωριστά subdomains για τη διαχείριση και τους εκπαιδευτές/καταρτιζόμενους.
            FRONTEND_DOMAIN=apousies.saek_example.gr
            ADMIN_DOMAIN=grammateia.saek_example.gr

3. Εκκίνηση
docker-compose up -d

4. Πρόσβαση
    Εκπαιδευτές/Φοιτητές: http://localhost:5411
    Γραμματεία/Admin: http://localhost:5411

5. Αρχική σύνδεση
    Admin: username admin, password admin (αλλάξτε το αμέσως)
    Εκπαιδευτές/Καταρτιζόμενοι: Σύνδεση με email + OTP (απαιτεί ρύθμιση email sender)

## Ρύθμιση Email (για OTP και αποστολή μαζικών email)
    Συνδεθείτε ως admin.
    Πηγαίνετε στις Ρυθμίσεις → Email.
    Προσθέστε έναν λογαριασμό αποστολέα (π.χ. Gmail με App Password).
    Ενεργοποιήστε τον λογαριασμό (checkbox "Ενεργός").
    Ορίστε το "Προς" και "CC" παραλήπτες (προαιρετικά).

## Εισαγωγή δεδομένων (CSV)

Ως admin, μπορείτε να εισάγετε:

    Εκπαιδευτές: surname, name, mail, phone

    Μαθήματα: specialty_name, semester, department, team, lesson_name, type_indicator, classroom, weekly_hours, surname, name

    Φοιτητές: amk, surname, name, father_name, mother_name, mail, phone, specialty_name, semester, department, team

Τα CSV αρχεία πρέπει να είναι σε UTF-8 με κόμμα (,) ως διαχωριστικό.

Δομή φακέλων

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

Συχνά προβλήματα

- "Database not available"
    Βεβαιωθείτε ότι το container saek_db τρέχει (docker ps).
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



ΣΑΕΚ - Σύστημα Απουσιολογίου & Βιβλίου Ύλης
