# The section 63 certificate: what the law asks for, and what software may do about it

Researched 2026-09-19. Two agents transcribed the statute independently from different copies of
the Gazette, a third researched case law and practice, and a reconciler compared them and
fact-checked our own product claims. The statutory text was then re-verified by hand against the
official Gazette PDF. **This is engineering research, not legal advice: an Indian litigation
advocate should review the template and every customer-facing claim before launch.**

Markers: **[V]** read in the primary source; **[S]** secondary report, not checked against the
primary text.

## What the statute says [V]

Bharatiya Sakshya Adhiniyam, 2023 (No. 47 of 2023), Gazette of India Extraordinary, Part II s.1,
No. 55, 25 December 2023 — <https://egazette.gov.in/WriteReadData/2023/250882.pdf>, SHA-256
`31ee68ea57b5303bf6697b5d8cb2004b9eb9180d7e9cbcbbdbf5bccab7cda5c1`. In force 1 July 2024.

- **s.63(1)–(3)** re-enact the old Indian Evidence Act s.65B(1)–(3): computer output is a document
  if four conditions hold — regular use by the person in lawful control, information regularly fed
  in, the device operating properly (or any fault not affecting the record), and the output derived
  from what was fed in.
- **s.63(4)** — where a statement is to be given in evidence *by virtue of this section*, a
  certificate "shall be submitted along with the electronic record **at each instance where it is
  being submitted for admission**". It identifies the record and how it was produced, gives device
  particulars, and deals with the (2) conditions; it is signed by "a person in charge of the
  computer or communication device or the management of the relevant activities (whichever is
  appropriate) **and an expert**"; matters may be stated "to the best of the knowledge and belief"
  of the signer, "in the certificate specified in the Schedule".
- **The Schedule** (pp. 46–47) is a fixed form in two parts. Part A is "(To be filled by the
  Party)"; Part B "(To be filled by the Expert)". Both identify the source device by tick box
  (Computer / Storage Media, DVR, Mobile, Flash Drive, CD/DVD, Server, Cloud, Other), give Make &
  Model, Color, Serial Number and IMEI/UIN/UID/MAC/Cloud ID, state "the HASH value/s" by ticking
  one of SHA1 / SHA256 / MD5 / Other "(Legally acceptable standard)", and carry "(Hash report to be
  enclosed with the certificate)". Only Part A contains the affirmation that the device was under
  lawful control and working properly, and the Owned / Maintained / Managed / Operated boxes.
  Both end with Date, **Time (IST) in 24-hour format**, and Place — the time of *signing*.
- The words CCTV, NVR and camera appear nowhere. The form does not say whether the hash is of the
  original on the recorder or of the copy produced.
- **s.141** — admissibility is for the judge. **s.170(2)** — proceedings pending on 1 July 2024
  continue under the Indian Evidence Act, so s.65B(4) applies to them instead.

## What the courts have said

- **Arjun Panditrao Khotkar v. Kailash Kushanrao Gorantyal, (2020) 7 SCC 1** [V] — on video
  recordings, under s.65B. The certificate is a condition precedent to admissibility and oral
  evidence cannot replace it; **producing the original device is a separate route that needs no
  certificate** (para 72(b)); anyone in a "responsible official position" for the device or the
  activity may sign (para 58); late production is at the court's discretion, not a right; and the
  Court called for chain-of-custody and metadata-preservation rules (para 72(c)–(d)).
- **Pune Bar Association v. Union of India, WP(C) 599/2026, order of 22 May 2026** [V] — the hash
  requirement and Part B are not manifestly arbitrary; a non-s.79A expert may sign Part B if the
  court is satisfied "on the basis of unimpeachable material"; the Madras High Court's narrower view
  is not binding precedent. **But the Court issued no notice and kept the question of law open.** It
  must not be described as having "upheld" s.63(4) after a hearing.
- **R v. B, Madras HC, 2024 SCC OnLine Mad 6084** [S] — required an IT Act s.79A notified examiner
  for Part B. Several High Courts have applied Arjun's reasoning to s.63 in 2026 [S].
- **Chandrabhan Sudam Sanap v. State of Maharashtra, 2025 INSC 116** [S] — station CCTV copied to
  pen drives with no certificate was excluded, and a death sentence set aside. The consequence of
  getting this wrong is not theoretical.

Unsettled: who may sign Part B; whether Part B is always needed; whether an export "from proper
custody" can be primary evidence under s.57; whether analytics output needs its own certificate.

## How the product maps onto the form

| Field | Who fills it | What the product does |
|---|---|---|
| Name, parentage, address | Signatory only | Nothing |
| Device tick box, Other | Signatory only | Particulars shown in an annex; never ticked |
| Make & Model, Serial, MAC/ID | Signatory (recorder details) | Left blank: the line means the *recorder*; we hold camera details, which go in an annex |
| Any other relevant information | Pre-filled, marked | Points to the particulars annex |
| Lawful-control affirmation | Signatory only | Printed verbatim; the facts that bear on it are listed in an annex for them to weigh |
| Owned / Maintained / Managed / Operated | Signatory only | Never ticked |
| Hash value | Pre-filled, marked | SHA-256 of each **original** file, by reference to the hash report |
| Algorithm | Pre-filled, marked | SHA256 ticked — the algorithm actually used |
| Hash report | Generated | Annex: file, size, SHA-256, when and by what it was computed |
| Date, Time (IST), Place | Signatory only | Never filled: they record the signing |
| **All of Part B** | Expert only | Left blank; a verification pack is annexed |

## Rules the generator follows

1. The form is reproduced word for word, tested against the Gazette text in CI.
2. Only the hash line and the "other information" pointer are pre-filled, in blue, marked "verify".
3. Derived material — frames, index records — is hashed but listed apart from the record, and never
   enters the hash line.
4. The hash report says the SHA-256 was computed by Smart Cam Monitoring on receipt, at a stated time
   and by a stated version, and that it identifies the file as received but does not by itself prove
   it matches the recorder's internal storage.
5. Every draft is titled a draft and watermarked "Not a certificate until affirmed and signed". A
   fresh, numbered draft is generated for each submission, and each is logged in the custody log.
6. The draft notes the s.236 BNS offence for a false declaration, the s.65B regime for pre-July-2024
   proceedings, and the original-device route; it tells the signer to have an advocate review it.
7. The product never signs, never applies a stored signature, and never names its vendor or staff as
   a signatory.

## Claims we must not make

"Court-admissible", "admissible", "legally valid", "court-filable", "BSA-compliant certificate",
"certified evidence", "tamper-proof", "forensically sound", "required for every electronic
record", and "nobody else does this". Admissibility is the court's decision, and the claims are
either wrong in law or wrong in fact: at least one Indian product (Chat2Evidence) already automates
s.63 certificates for WhatsApp evidence, and free templates are widespread.

What we can say: *prepares a pre-filled draft of the BSA 2023 section 63(4) Schedule certificate,
with a SHA-256 hash report and chain-of-custody log, for review and signature by the person in
charge of your recorder and an expert. Whether the record is admitted is decided by the court.*

## Sources

- Gazette of India, BSA 2023: <https://egazette.gov.in/WriteReadData/2023/250882.pdf>; MHA copy
  <https://www.mha.gov.in/sites/default/files/2024-04/250882_english_01042024_0.pdf>
- Arjun Panditrao Khotkar (2020) 7 SCC 1: <https://aphc.gov.in/docs/imp_judgements/Arjun%20Panditrao%20Khotkar%20_%20Kailash%20Kushanrao%20Gorantyal%20And%20Ors._1701334263.pdf>
- Pune Bar Association v. Union of India, order of 22 May 2026: <https://www.livelaw.in/pdf_upload/2026/05/27/pune-bar-association-v-union-of-india-676590.pdf>
- R v. B (Madras HC) report: <https://www.livelaw.in/high-court/madras-high-court/madras-high-court-section-63-bsa-meity-notify-experts-within-3-months-274015>
- Chandrabhan Sudam Sanap, 2025 INSC 116: <https://indiankanoon.org/doc/61280287/>
- Bharatiya Nyaya Sanhita, 2023, s.236: <https://www.mha.gov.in/sites/default/files/250883_english_01042024.pdf>
- BPR&D SOP on audio-visual recording and hashing: <https://bprd.nic.in/uploads/pdf/1723616060_2a3a0a30527ffcffc634.pdf>
- Competitor evidence: <https://chat2evidence.in/public/pages/section-63-bsa-certificate-template-download>
