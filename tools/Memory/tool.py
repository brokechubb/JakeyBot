from tools.Memory.manifest import ToolManifest
from core.ai.history import History
from datetime import datetime, timedelta, timezone
from os import environ
import logging
import re


class Tool(ToolManifest):
    # Required attributes for bot system recognition
    tool_human_name = "Memory Recall"

    def __init__(self, method_send, discord_ctx, discord_bot):
        super().__init__()
        self.method_send = method_send
        self.discord_ctx = discord_ctx
        self.discord_bot = discord_bot
        self.history_db = None

        # Set the schema attributes for this instance
        self.tool_schema = self.tool_schema_basic
        self.tool_schema_openai = self.tool_schema_openai

        # Try to get the shared database connection from the bot
        # This is the proper way to access the shared database connection
        if hasattr(discord_bot, "DBConn") and isinstance(discord_bot.DBConn, History):
            self.history_db = discord_bot.DBConn
            logging.info("Memory tool using shared database connection")
        else:
            # Fallback: try to get from cogs if available
            try:
                # Look for the Misc cog which has the database connection
                misc_cog = discord_bot.get_cog("Misc")
                if misc_cog and hasattr(misc_cog, "DBConn"):
                    self.history_db = misc_cog.DBConn
                    logging.info("Memory tool using database connection from Misc cog")
                else:
                    # Last resort: create a new connection
                    from core.services.helperfunctions import HelperFunctions
                    from motor.motor_asyncio import AsyncIOMotorClient
                    from os import environ

                    db_conn = AsyncIOMotorClient(environ.get("MONGO_DB_URL"))
                    self.history_db = History(bot=discord_bot, db_conn=db_conn)
                    logging.info("Memory tool created new database connection")
            except Exception as e:
                logging.error(
                    f"Failed to initialize Memory tool database connection: {e}"
                )
                self.history_db = None

    async def _tool_function_remember_fact(
        self, fact: str, category: str = None, expires_in: str = None
    ):
        """Store a new fact in memory"""
        if not self.history_db:
            return "❌ Memory system is not available at the moment"

        try:
            # Determine guild/user ID using the same logic as the chat system
            if environ.get("SHARED_CHAT_HISTORY", "false").lower() == "true":
                guild_id = (
                    self.discord_ctx.guild.id
                    if self.discord_ctx.guild
                    else self.discord_ctx.author.id
                )  # Always fallback to ctx.author.id for DMs since ctx.guild is None
            else:
                guild_id = self.discord_ctx.author.id

            user_id = self.discord_ctx.author.id

            # Get username for prepending to fact
            username = self.discord_ctx.author.display_name or f"User{user_id}"

            # Parse expiration time
            expires_at = None
            if expires_in and expires_in.lower() != "never":
                try:
                    now = datetime.now(timezone.utc)
                    if expires_in.endswith("d"):
                        days = int(expires_in[:-1])
                        expires_at = now + timedelta(days=days)
                    elif expires_in.endswith("h"):
                        hours = int(expires_in[:-1])
                        expires_at = now + timedelta(hours=hours)
                    elif expires_in.endswith("m"):
                        minutes = int(expires_in[:-1])
                        expires_at = now + timedelta(minutes=minutes)
                    else:
                        raise ValueError("Invalid time format")
                except:
                    return "⚠️ Invalid expiration format. Use number followed by d, h, or m (e.g., 1d, 2h, 30m), or 'never' for permanent"

            # Prepend username to fact text to associate information with specific user
            fact_with_user = f"[{username}] {fact}"

            # Add category to fact text if provided
            if category:
                fact_with_category = f"[{category}] {fact_with_user}"
            else:
                fact_with_category = fact_with_user

            # Store the fact
            fact_id = await self.history_db.add_fact(
                guild_id=guild_id,
                user_id=user_id,
                fact_text=fact_with_category,
                source=f"memory_tool/{user_id}",
                expires_at=expires_at,
            )

            response = f"✅ I'll remember that: **{fact}**"
            if category:
                response += f" (Category: {category})"
            if expires_at:
                response += f"\n⏰ Expires: {expires_at.strftime('%Y-%m-%d %H:%M')} UTC"
            else:
                response += " (Permanent)"

            # Return a natural response that the AI can use
            return f"I've remembered that {fact}. This information is now stored in my memory for future conversations."

        except Exception as e:
            logging.error(f"Error storing fact: {e}")
            return f"❌ Failed to store fact: {str(e)}"

    async def _tool_function_recall_fact(self, query: str, limit: int = 3):
        """Search for and retrieve relevant facts from memory"""
        if not self.history_db:
            return "❌ Memory system is not available at the moment"

        try:
            # Determine guild/user ID using the same logic as the chat system
            if environ.get("SHARED_CHAT_HISTORY", "false").lower() == "true":
                guild_id = (
                    self.discord_ctx.guild.id
                    if self.discord_ctx.guild
                    else self.discord_ctx.author.id
                )  # Always fallback to ctx.author.id for DMs since ctx.guild is None
            else:
                guild_id = self.discord_ctx.author.id

            current_user_id = self.discord_ctx.author.id

            # Limit the search results
            limit = min(max(1, limit), 10)

            # NEW: Search global facts first (guild_id = 0)
            global_facts = []
            try:
                global_collection = self.history_db._db["knowledge_0"]
                # Search for global facts that match the query
                async for fact in global_collection.find(
                    {"$text": {"$search": query}}
                ).limit(limit):
                    if fact and (
                        fact.get("expires_at") is None
                        or fact["expires_at"] > datetime.now(timezone.utc)
                    ):
                        # Check if this is a public global fact
                        fact_text = fact.get("fact_text", "")
                        if "[SYSTEM_GLOBAL]" in fact_text:
                            global_facts.append(fact_text)
            except Exception as e:
                logging.debug(f"Global fact search failed: {e}")

            # FIRST PRIORITY: Search for facts created by the current user
            user_facts = await self.history_db.search_facts_by_user(
                guild_id, current_user_id, query, limit=limit
            )

            # SECOND PRIORITY: If no user-specific facts found, search all facts
            all_facts = []
            if not user_facts:
                all_facts = await self.history_db.search_facts(
                    guild_id, query, limit=limit
                )

            # THIRD PRIORITY: If still no facts, get recent facts by the current user
            recent_user_facts = []
            if not user_facts and not all_facts:
                recent_user_facts = await self.history_db.get_facts_by_user(
                    guild_id, current_user_id, limit=limit
                )

            # FOURTH PRIORITY: If still nothing, get recent facts from anyone
            recent_all_facts = []
            if not user_facts and not all_facts and not recent_user_facts:
                recent_all_facts = await self.history_db.get_recent_facts(
                    guild_id, limit=limit
                )

            # Combine all facts with priority: global facts first, then user facts, then others
            all_relevant_facts = []
            
            # Add global facts first
            for fact in global_facts:
                # Clean up the fact text for display
                clean_fact = fact.replace("[SYSTEM_GLOBAL]", "").replace("[SYSTEM_PRIVATE]", "")
                if clean_fact not in [f[0] for f in all_relevant_facts]:
                    all_relevant_facts.append((clean_fact, "global knowledge base"))

            # Add user facts
            for fact in user_facts:
                if fact not in [f[0] for f in all_relevant_facts]:
                    all_relevant_facts.append((fact, "your personal memory"))

            # Add general facts
            for fact in all_facts:
                if fact not in [f[0] for f in all_relevant_facts]:
                    all_relevant_facts.append((fact, "general memory"))

            # Limit to requested number
            all_relevant_facts = all_relevant_facts[:limit]

            if not all_relevant_facts:
                return f"🤔 I couldn't find any facts matching '{query}' in my memory"

            # Format the response with source information
            if len(all_relevant_facts) == 1:
                fact_text, source_info = all_relevant_facts[0]
                return f"I found this information in my memory from {source_info}: {fact_text}"
            else:
                facts_with_sources = []
                for fact_text, source_info in all_relevant_facts:
                    facts_with_sources.append(f"{fact_text} (from {source_info})")
                facts_text = "; ".join(facts_with_sources)
                return f"I found these details in my memory: {facts_text}"

        except Exception as e:
            logging.error(f"Error recalling facts: {e}")
            return f"❌ Failed to recall facts: {str(e)}"

    async def _tool_function_list_facts(self, category: str = None, limit: int = 10):
        """List all facts in a specific category or all facts"""
        if not self.history_db:
            return "❌ Memory system is not available at the moment"

        try:
            # Determine guild/user ID using the same logic as the chat system
            if environ.get("SHARED_CHAT_HISTORY", "false").lower() == "true":
                guild_id = (
                    self.discord_ctx.guild.id
                    if self.discord_ctx.guild
                    else self.discord_ctx.author.id
                )  # Always fallback to ctx.author.id for DMs since ctx.guild is None
            else:
                guild_id = self.discord_ctx.author.id

            current_user_id = self.discord_ctx.author.id

            # Limit the results
            limit = min(max(1, limit), 20)

            # Get facts from the knowledge base
            knowledge_collection = self.history_db._db[f"knowledge_{guild_id}"]

            # Build query - prioritize current user's facts
            query = {}
            if category:
                query["fact_text"] = {
                    "$regex": f"^\\[{re.escape(category)}\\]",
                    "$options": "i",
                }

            # Get facts - prioritize current user
            facts = []

            # First get facts by current user
            user_query = query.copy()
            user_query["user_id"] = current_user_id

            async for fact in knowledge_collection.find(user_query).limit(limit):
                if fact and (
                    fact.get("expires_at") is None
                    or fact["expires_at"] > datetime.now(timezone.utc)
                ):
                    facts.append(fact["fact_text"])

            # If we need more facts and category is specified, get other users' facts in that category
            if len(facts) < limit and category:
                other_query = query.copy()
                other_query["user_id"] = {"$ne": current_user_id}

                async for fact in knowledge_collection.find(other_query).limit(
                    limit - len(facts)
                ):
                    if fact and (
                        fact.get("expires_at") is None
                        or fact["expires_at"] > datetime.now(timezone.utc)
                    ):
                        fact_text = fact["fact_text"]
                        if fact_text not in facts:  # Avoid duplicates
                            facts.append(fact_text)

            if not facts:
                if category:
                    return f"🤔 I don't have any facts in the '{category}' category"
                else:
                    return "🤔 I don't have any facts stored in my memory yet"

            # Format the response
            if category:
                response = (
                    f"📋 Facts in category '{category}' (prioritizing your facts):\n"
                )
            else:
                response = f"📋 All facts in my memory (prioritizing your facts):\n"

            for i, fact in enumerate(facts, 1):
                response += f"{i}. {fact}\n"

            if len(facts) == limit:
                response += f"\n... and more (showing first {limit})"

            # Return the facts in a natural format for the AI to use
            if len(facts) == 1:
                return f"I have one fact stored: {facts[0]}"
            else:
                facts_text = "; ".join(facts)
                return f"I have {len(facts)} facts stored: {facts_text}"

        except Exception as e:
            logging.error(f"Error listing facts: {e}")
            return f"❌ Failed to list facts: {str(e)}"

    async def _tool_function_my_facts(self, limit: int = 10):
        """List facts created by the current user"""
        if not self.history_db:
            return "❌ Memory system is not available at the moment"

        try:
            # Determine guild/user ID using the same logic as the chat system
            if environ.get("SHARED_CHAT_HISTORY", "false").lower() == "true":
                guild_id = (
                    self.discord_ctx.guild.id
                    if self.discord_ctx.guild
                    else self.discord_ctx.author.id
                )  # Always fallback to ctx.author.id for DMs since ctx.guild is None
            else:
                guild_id = self.discord_ctx.author.id

            current_user_id = self.discord_ctx.author.id

            # Limit the results
            limit = min(max(1, limit), 20)

            # Get facts by current user
            facts = await self.history_db.get_facts_by_user(
                guild_id, current_user_id, limit=limit
            )

            if not facts:
                return "🤔 You haven't told me any facts to remember yet."

            # Format the response
            response = f"📋 Facts you've told me to remember:\n"
            for i, fact in enumerate(facts, 1):
                response += f"{i}. {fact}\n"

            if len(facts) == limit:
                response += f"\n... and more (showing first {limit})"

            # Return the facts in a natural format for the AI to use
            if len(facts) == 1:
                return f"You've told me one fact: {facts[0]}"
            else:
                facts_text = "; ".join(facts)
                return f"You've told me {len(facts)} facts: {facts_text}"

        except Exception as e:
            logging.error(f"Error listing user facts: {e}")
            return f"❌ Failed to list your facts: {str(e)}"

    async def _tool_function_forget_fact(self, query: str):
        """Forget a specific fact (only if created by the current user)"""
        if not self.history_db:
            return "❌ Memory system is not available at the moment"

        try:
            # Determine guild/user ID using the same logic as the chat system
            if environ.get("SHARED_CHAT_HISTORY", "false").lower() == "true":
                guild_id = (
                    self.discord_ctx.guild.id
                    if self.discord_ctx.guild
                    else self.discord_ctx.author.id
                )  # Always fallback to ctx.author.id for DMs since ctx.guild is None
            else:
                guild_id = self.discord_ctx.author.id

            current_user_id = self.discord_ctx.author.id

            # Search for facts by current user that match the query
            user_facts = await self.history_db.search_facts_by_user(
                guild_id, current_user_id, query, limit=5
            )

            if not user_facts:
                return (
                    f"🤔 I couldn't find any facts matching '{query}' that you created."
                )

            # For now, just return the facts found (actual deletion would need to be implemented)
            if len(user_facts) == 1:
                response = f"📝 I found this fact you created: **{user_facts[0]}**\n\n*Note: Fact deletion is not yet implemented, but I can see what you want me to forget.*"
            else:
                response = f"📝 I found these facts you created matching '{query}':\n"
                for i, fact in enumerate(user_facts, 1):
                    response += f"{i}. **{fact}**\n"
                response += "\n*Note: Fact deletion is not yet implemented, but I can see what you want me to forget.*"

            return response

        except Exception as e:
            logging.error(f"Error searching for facts to forget: {e}")
            return f"❌ Failed to search for facts: {str(e)}"


def setup(bot):
    bot.add_cog(Tool(bot))
