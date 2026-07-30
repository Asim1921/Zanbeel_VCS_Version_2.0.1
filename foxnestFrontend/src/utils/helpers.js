// Utility functions for the application

export const formatDate = (date) => {
  return new Date(date).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric'
  })
}

export const formatFileSize = (bytes) => {
  if (bytes === 0) return '0 Bytes'
  
  const k = 1024
  const sizes = ['Bytes', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

export const truncateText = (text, maxLength) => {
  if (text.length <= maxLength) return text
  return text.substr(0, maxLength) + '...'
}

export const generateAvatar = (name) => {
  return name
    .split(' ')
    .map(word => word.charAt(0))
    .join('')
    .toUpperCase()
    .slice(0, 2)
}

export const getLanguageColor = (language) => {
  const colors = {
    JavaScript: '#f1e05a',
    TypeScript: '#3178c6',
    Python: '#3572A5',
    Java: '#b07219',
    'C++': '#f34b7d',
    C: '#555555',
    'C#': '#239120',
    PHP: '#4F5D95',
    Ruby: '#701516',
    Go: '#00ADD8',
    Rust: '#dea584',
    Swift: '#fa7343',
    Kotlin: '#A97BFF',
    'React Native': '#61dafb',
    Flutter: '#02569B',
    HTML: '#e34c26',
    CSS: '#1572B6',
    Vue: '#4FC08D',
    Angular: '#DD0031',
    'Node.js': '#339933',
    SQL: '#e38c00',
    Shell: '#89e051',
    PowerShell: '#012456',
    Bash: '#89e051',
    Docker: '#2496ED',
    YAML: '#cb171e',
    JSON: '#292929',
    Markdown: '#083fa1'
  }
  
  return colors[language] || '#858585'
}
