import Image from "next/image";
export function Brand({ lockup = false }: { lockup?: boolean }) {
  return (
    <div className="brand">
      {lockup ? (
        <Image
          src="/brand/quantix-lockup.png"
          alt="QUANTIX"
          width={260}
          height={72}
          priority
          unoptimized
          className="quantix-lockup"
        />
      ) : (
        <>
          <Image
            src="/brand/quantix-symbol.png"
            alt=""
            width={38}
            height={38}
            priority
            unoptimized
            className="quantix-symbol"
          />
          <Image
            src="/brand/quantix-wordmark.png"
            alt="QUANTIX"
            width={135}
            height={36}
            priority
            unoptimized
            className="quantix-wordmark"
          />
        </>
      )}
    </div>
  );
}
