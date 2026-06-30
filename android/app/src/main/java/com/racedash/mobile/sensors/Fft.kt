package com.racedash.mobile.sensors

/** Minimal in-place iterative radix-2 FFT (no external dependency). */
object Fft {
    /**
     * In-place complex FFT. [re] and [im] must be the same power-of-two length.
     */
    fun transform(re: DoubleArray, im: DoubleArray) {
        val n = re.size
        require(n and (n - 1) == 0) { "FFT size must be a power of two (got $n)" }
        if (n <= 1) return

        // Bit-reversal permutation.
        var j = 0
        for (i in 1 until n) {
            var bit = n shr 1
            while (j and bit != 0) {
                j = j xor bit
                bit = bit shr 1
            }
            j = j or bit
            if (i < j) {
                val tr = re[i]; re[i] = re[j]; re[j] = tr
                val ti = im[i]; im[i] = im[j]; im[j] = ti
            }
        }

        // Danielson-Lanczos.
        var len = 2
        while (len <= n) {
            val ang = -2.0 * Math.PI / len
            val wRe = Math.cos(ang)
            val wIm = Math.sin(ang)
            var i = 0
            while (i < n) {
                var curRe = 1.0
                var curIm = 0.0
                for (k in 0 until len / 2) {
                    val aRe = re[i + k]
                    val aIm = im[i + k]
                    val bRe = re[i + k + len / 2]
                    val bIm = im[i + k + len / 2]
                    val tRe = bRe * curRe - bIm * curIm
                    val tIm = bRe * curIm + bIm * curRe
                    re[i + k] = aRe + tRe
                    im[i + k] = aIm + tIm
                    re[i + k + len / 2] = aRe - tRe
                    im[i + k + len / 2] = aIm - tIm
                    val nextRe = curRe * wRe - curIm * wIm
                    curIm = curRe * wIm + curIm * wRe
                    curRe = nextRe
                }
                i += len
            }
            len = len shl 1
        }
    }
}
