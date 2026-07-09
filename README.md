# Gemini
Repository that will be hosting gemini (micro-service), will support parallel processing workers, multiple API Keys, will return which API key is dead, rate limiting
Input is prompt, media, model type and other parameters
Output will be input tokens, output tokens and the response, also it will be tracking the usage of all the API keys in the .env and all the logs will be available in tmp/ai or some similar folder structure

(update readme in case of feature addition) 
(above is just the basic layout)
(in case of 429 it should also return after how much time that API key can be useful again, block should not exceed 1 hour per api key in case the key is overused too much)


Also try to keep it generic like it should be able to support all the models not only gemini, anthropic, whatever, can take inspiration from that 1.6 Billion Free tokens thing
