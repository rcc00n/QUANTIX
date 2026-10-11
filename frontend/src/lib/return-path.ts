import { navigation } from "@/lib/navigation";

/** Only an existing local dashboard path can follow successful authentication. */
export function safeReturnPath(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.length > 2048)
    return null;
  try {
    const url = new URL(value, "https://quantix.invalid");
    if (
      url.origin !== "https://quantix.invalid" ||
      !navigation.some((item) => url.pathname === `/${item.slug}`)
    )
      return null;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}
