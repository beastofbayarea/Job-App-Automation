function ConvertTo-PosixShellLiteral {
    param(
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value.Contains([char]0) -or $Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "POSIX shell arguments cannot contain null or newline characters."
    }

    $SingleQuote = [string][char]39
    $DoubleQuote = [string][char]34
    $EmbeddedSingleQuote = $SingleQuote + $DoubleQuote + $SingleQuote + $DoubleQuote + $SingleQuote
    return $SingleQuote + $Value.Replace($SingleQuote, $EmbeddedSingleQuote) + $SingleQuote
}
