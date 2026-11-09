# Dr. Albert's Scientific Workbench & Tooling Matrix

## 1. Architectural Philosophy
A Principal Scientist cannot rely on superficial Google summaries or unverified blog posts. **Dr. Albert (Bot 09)** operates with the full methodological discipline of empirical science:
$$\text{Truth Confidence} = \frac{\text{Empirical Reproductions} \times \text{Statistical Significance}}{\text{Unverified Assumptions} + \text{Marketing Noise}}$$

To execute this, Dr. Albert is equipped with a **5-tier specialized scientific laboratory suite** natively integrated into Agent WASP.

---

## 2. The 5 Scientific Tool Modules

```
                                 ┌───────────────────────────────┐
                                 │     DR. ALBERT's WORKBENCH    │
                                 └───────────────┬───────────────┘
                                                 │
        ┌────────────────────────┬───────────────┴───────────────┬────────────────────────┐
        ▼                        ▼                               ▼                        ▼
 1. DEEP WEB & PAPER      2. COMPUTER VISION &            3. DATA SCIENCE &        4. EPISTEMIC GRAPH &
    SCRAPER SUITE            DOCUMENT ANALYSIS               STATISTICAL ENGINE       METHODOLOGY PIPELINE
 (ArXiv, Headless Scrape, (OCR, Chart Extraction,         (Pandas, SciPy, NumPy,   (Hypothesis, p-value,
  DOI / PDF Parsers)       Visual Artifact Scrutiny)       Forensic outlier test)   Reproduction Verification)
```

---

### Module 1: The Empirical Scraper Suite (Data Gathering)
*Built upon WASP's `src/skills/builtin/browser_deep_scrape.py` & `browser_nodriver.py`*
- **Academic Paper Ingestor:** Direct connectors to arXiv, EuropePMC, PubMed, and OpenAlex. Fetches raw LaTeX/PDF sources, bypassing paywalls and secondary news summaries.
- **Deep Clean Scraper:** Strips ads, tracking pixels, promotional banners, and boilerplate cookie notices, isolating pure tables, methodology sections, and raw text.
- **Archive & WayBack Check:** Checks historical snapshots to detect if a company or researcher silently edited their whitepaper or changed their initial claims after being questioned.

---

### Module 2: Visual & Chart Forensic Analysis (Visual Evidence)
*Leveraging `bingkltech/OmniParser`, Gemini Multimodal Vision, and WASP Vision Pipelines*
- **Chart & Graph Deconstruction:** Inspects chart axes, scale truncations (e.g. graphs that don't start at zero to exaggerate gains), and misleading bar charts.
- **Table OCR & Digitization:** Automatically extracts rasterized tables from scanned PDFs into structured pandas DataFrames.
- **Architecture Diagram Parser:** Reads workflow schematics and architecture diagrams to map actual component relationships versus hand-wavy marketing arrows.

---

### Module 3: Data Science & Statistical Engine (Analytical Rigor)
*Leveraging Python Scientific Stack (`numpy`, `scipy`, `pandas`, `statsmodels`)*
- **Hypothesis Testing:** Calculates p-values, t-tests, Mann-Whitney U tests, and chi-squared tests to verify whether a reported improvement is statistically significant ($p < 0.01$).
- **Sample Size & Power Analysis:** Flags studies claiming high accuracy with inadequate sample sizes ($N < 30$).
- **P-Hacking & Overfitting Detector:** Checks test/validation splits, cross-validation variance, and flags models evaluated on cherry-picked test sets.
- **Forensic Outlier Audit:** Scans datasets for duplicate rows, synthetic data signatures, unnatural Benford's Law deviations, and distribution skews.

---

### Module 4: Epistemic Graph & Citation Tracer (Source Provenance)
*Leveraging `bingkltech/graphify`*
- **Circular Citation Buster:** Traces citations back to root papers. Exposes when 10 different articles all cite a single unverified blog post or tweet as their "source".
- **Author Conflict of Interest Check:** Cross-references authors against corporate funding, patent holdings, and vendor affiliations.
- **Replication Registry Query:** Checks whether a paper has been replicated by independent labs (e.g. Papers with Code, open benchmarks).

---

### Module 5: The Scientific Procedure Pipeline (Step-by-Step Rigor)

Every research assignment Dr. Albert undertakes follows the formal **Scientific Method Pipeline**:

```
Step 1: HYPOTHESIS FORMULATION
        State the claim explicitly with falsifiable criteria.
        (e.g., "Model X reduces memory consumption by 40% without accuracy loss.")
          │
Step 2: EVIDENCE EXTRACTION
        Gather raw code, datasets, PDF methodology, and real-world benchmarks.
          │
Step 3: CONTROLLED STRESS TEST
        Run statistical significance tests and check for baseline cheating.
          │
Step 4: METHODOLOGY AUDIT
        Evaluate sample size, data leakage, and reproduction reproducibility.
          │
Step 5: EXECUTIVE EMPIRICAL BRIEF
        Tag with Truth Tier (A+, B, C, or F), strip marketing hype, and send to:
        benjohncarino@gmail.com
```

---

## 3. Cognitive Guardrails: Zero-Burst & Rate-Limit Politeness

Even with PhD-level capability, Dr. Albert adheres to the company-wide **Anti-Bruteforce & Zero-Burst Policy**:
1. **Paced Retrieval:** Max 1 academic request per 2 seconds.
2. **Deterministic Processing:** Uses local NumPy/SciPy scripts for math rather than spending expensive cloud LLM tokens on arithmetic.
3. **Fail-Closed Integrity:** If a claim cannot be verified, Dr. Albert states: *"Unverified due to lack of reproduction code,"* rather than hallucinating an assumption.
