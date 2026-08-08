// src/config.js

/**
 * APPLICATION CONFIGURATION & SINGLE SOURCE OF TRUTH
 * * This file configures:
 * 1. Global Site Metadata (Name, URLs, Socials)
 * 2. Contentful API Credentials (Read-Only/Delivery API)
 * 3. SEO & Routing Rules for Sitemap Generation
 * * NOTE: Use CommonJS (module.exports) so this file can be consumed by 
 * both the React Frontend (Webpack) and Node.js Build Scripts.
 */

const DEFAULT_ENV = 'master';

module.exports = {
  // ============================
  // 1. Global Site Settings
  // ============================
  site: {
    name: 'Cent Capital',
    baseUrl: 'https://www.cent.capital',
    contactEmail: 'hello@cent.capital',
    supportPhone: '(202) 630-3211',
    address: '169 Madison Ave STE 38242, New York, NY 10016',
    
    // Social Media Links (SSOT)
    social: {
      twitter: 'https://x.com/BeastofBayArea',
      linkedin: 'https://www.linkedin.com/company/cent-capital',
      youtube: 'https://www.youtube.com/@centcapglobal',
      instagram: 'https://www.instagram.com/cent_capital',
    }
  },

  // ============================
  // 2. Contentful Configuration
  // ============================
  // Note: API Tokens here are Delivery API (Read-Only). 
  // It is safe to expose these in client-side code, but ideally use .env variables.
  contentful: {
    // 1. Finpedia (General Encyclopedia)
    finpedia: {
      space: process.env.REACT_APP_CONTENTFUL_SPACE_ID || 'eadiri1o5i82',
      accessToken: process.env.REACT_APP_CONTENTFUL_ACCESS_TOKEN || 'Mq9fuRLi1qy2ZBCqmNT-p1LmHxGKknWUmDU8mr8h54U',
      environment: process.env.REACT_APP_CONTENTFUL_ENVIRONMENT || DEFAULT_ENV
    }
  },

  // ============================
  // 3. Routing & SEO Rules
  // ============================
  // Used by: scripts/generate-sitemap.js
  routes: {
    // Routes that should be ignored by sitemaps/crawlers
    excluded: ['verify-email', 'forgot-password', 'reset-password', '*', '404', 'admin'],
    
    // Routes behind authentication (No Index / No Follow)
    protected: [
      '/dashboard', 
      '/insights', 
      '/spending', 
      '/hidden-insights', 
      '/profile', 
      '/subscription/plans', 
      '/subscription/success', 
      '/complete-profile'
    ],

    // SEO Priorities and Change Frequencies
    // This defines the "Static" portion of your sitemap.
    metadata: {
      // Core Landing
      '/':                 { priority: 1.0, changefreq: 'weekly' },
      
      // Feature Hubs
      '/finpedia':         { priority: 0.9, changefreq: 'daily' },
      
      // Corporate / Static
      '/about':            { priority: 0.8, changefreq: 'monthly' },
      '/partnerships':     { priority: 0.7, changefreq: 'monthly' },
      '/pledge':           { priority: 0.6, changefreq: 'yearly' },
      '/press':            { priority: 0.6, changefreq: 'weekly' },
      
      // Legal & Support
      '/contact':          { priority: 0.6, changefreq: 'yearly' },
      '/faq':              { priority: 0.5, changefreq: 'monthly' },
      '/privacy':          { priority: 0.3, changefreq: 'yearly' },
      '/terms':            { priority: 0.3, changefreq: 'yearly' },
      
      // Conversion
      '/signup':           { priority: 0.8, changefreq: 'monthly' },
      
      // Fallback
      default:             { priority: 0.5, changefreq: 'monthly' }
    }
  },

  // ============================
  // 4. Google Cloud Platform (GCP) Configuration
  // ============================
  // Used by: scripts/submit-google-indexing.js
  // Project: Cent Capital
  gcp: {
    projectId: 'cent-capital-472820',
    projectNumber: '158183855680',

    // Service Account 1: cent-capital-1 — used for Google Search Console & Indexing API
    serviceAccount: {
      name: 'cent-capital-1',
      email: 'cent-capital-1@cent-capital-472820.iam.gserviceaccount.com',
      clientId: '105595149077633882961',
      keyId: 'b18e17e354b9393c58fca86ea49ca9bb22699a54',
      // JSON key file — private repo only
      keyFilePath: 'src/cent-capital-472820-b18e17e354b9.json'
    },

    // Service Account 2: GeminiApiKey1 — used for Gemini AI features
    geminiServiceAccount: {
      name: 'GeminiApiKey1',
      email: 'ais-gemini-key-6896b1ead19148d@158183855680.iam.gserviceaccount.com',
      clientId: '107911319197548551539',
      keyId: 'f55ada69e99b37756a0d71e2560e8bc2933b70bf',
      keyFilePath: 'src/cent-capital-472820-f55ada69e99b.json'
    },

    // Google Search Console (GSC) linked service account (Owner)
    gscServiceAccount: 'gsc-indexing-bot@cent-capital-gsc.iam.gserviceaccount.com',

    // Google Indexing API settings
    indexingApi: {
      endpoint: 'https://indexing.googleapis.com/v3/urlNotifications:publish',
      scopes: ['https://www.googleapis.com/auth/indexing'],
      // Default daily quota: 200 URLs/day. Expandable via quota request.
      dailyQuota: 200,
      // Number of priority URLs submitted per seo:accelerate run
      batchSize: 100
    }
  },

  // ============================
  // 5. SEO & Indexing Services
  // ============================
  seo: {
    // IndexNow — notifies Bing, Yandex, Naver, Seznam instantly
    indexNow: {
      key: '89eedac99a4441aaa8c4ec3516b6855c',
      keyLocation: 'https://www.cent.capital/89eedac99a4441aaa8c4ec3516b6855c.txt',
      endpoints: [
        'https://api.indexnow.org/indexnow',
        'https://www.bing.com/indexnow',
        'https://yandex.com/indexnow'
      ]
    },

    // RSS Feed
    rss: {
      feedUrl: 'https://www.cent.capital/rss.xml',
      itemCount: 200
    },

    // Sitemaps
    sitemaps: {
      index: 'https://www.cent.capital/sitemap-index.xml',
      main: 'https://www.cent.capital/sitemap.xml',
      finpedia: 'https://www.cent.capital/sitemap-finpedia.xml'
    }
  }
};
