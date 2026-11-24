# Third-party assets and references

- Supplied QUANTIX PNG assets are retained unchanged in QUANTIX_brand_assets and
  copied into frontend/public/brand for application use.
- scripts/passphrase_words.txt is the unmodified EFF Short Wordlist #1 by Joseph
  Bonneau / Electronic Frontier Foundation, obtained from
  https://www.eff.org/files/2016/09/08/eff_short_wordlist_1.txt . Attribution and
  CC BY terms: https://www.eff.org/copyright . Source guidance: https://www.eff.org/dice .
  Six independent selections use Python secrets, plus four digits from 23456789.
  The dictionary is public data; generated passwords are never stored with it.
