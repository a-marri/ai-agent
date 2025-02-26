import os
import discord
from discord.ext import commands
import random
from enum import Enum
from typing import Dict, List, Optional, Union
import asyncio
from dotenv import load_dotenv
from agent import MistralAgent

# Load environment variables
load_dotenv()

# Initialize Mistral client
MISTRAL_MODEL = "mistral-large-latest"

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

class PlayerRole(Enum):
    VILLAGER = "Villager"
    MAFIA = "Mafia"
    DETECTIVE = "Detective"
    DOCTOR = "Doctor"

class GameState(Enum):
    WAITING = "waiting"
    NIGHT = "night"
    DAY = "day"
    VOTING = "voting"

class StoryTeller:
    def __init__(self):
        self.agent = MistralAgent()
        
    async def generate_story(self, prompt: str, max_length: int = 1900) -> str:
        """Generate a story using Mistral AI"""
        try:
            # Create a mock discord message for the agent
            mock_message = type('MockMessage', (), {'content': prompt})()
            response = await self.agent.run(mock_message)
            
            if not response:
                return "The village continues its story..."  # Fallback if empty response
            
            # If response is too long, ask Mistral to shorten it
            if len(response) > max_length:  # Leave buffer for safety
                shorten_prompt = f"Please shorten this story while keeping the main dramatic elements (keep it under {max_length} characters):\n\n{response}"
                mock_message.content = shorten_prompt
                response = await self.agent.run(mock_message)
                
                if not response or len(response) > max_length:
                    # If still too long or empty, use first max_length chars as fallback
                    return response[:max_length]
                    
            return response
                
        except Exception as e:
            print(f"Error generating story: {e}")
            return "The village continues its story..."  # Fallback message if anything goes wrong

class NPCPlayer:
    def __init__(self, name: str, player_id: int):
        self.name = name
        self.id = player_id
        
    async def send(self, message: str):
        # NPCs don't need to receive messages
        pass

class PollView(discord.ui.View):
    def __init__(self, timeout=30):
        super().__init__(timeout=timeout)
        self.votes = {}
        
    @discord.ui.select(
        placeholder="Choose a player...",
        min_values=1,
        max_values=1,
        options=[]  # Will be set when creating the view
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        """Called when a user makes a selection"""
        # Record the vote (overwrite if user votes again)
        self.votes[interaction.user.id] = int(select.values[0])
        await interaction.response.send_message("Your vote has been recorded!", ephemeral=True)
        
    async def on_timeout(self):
        """Called when the view times out"""
        # Disable all items in the view
        for item in self.children:
            item.disabled = True
        # The view will be updated in get_poll_result

class MafiaGame:
    def __init__(self):
        self.players: Dict[int, Union[discord.Member, NPCPlayer]] = {}
        self.player_roles: Dict[int, PlayerRole] = {}
        self.state: GameState = GameState.WAITING
        self.alive_players: List[int] = []
        self.mafia_channel: Optional[discord.TextChannel] = None
        self.current_votes: Dict[int, int] = {}
        self.night_actions: Dict[int, int] = {}
        self.main_channel: Optional[discord.TextChannel] = None
        self.dead_players: List[int] = []
        self.npc_base_id = 1000000  # Base ID for NPCs to avoid conflicts
        self.npc_count = 0
        self.detective_channel: Optional[discord.TextChannel] = None
        self.doctor_channel: Optional[discord.TextChannel] = None
        
        # Initialize storyteller
        self.storyteller = StoryTeller()
        
        # Store active poll views
        self.active_polls: Dict[int, PollView] = {}
        
        # Cache static role instructions
        self.role_instructions = {
            'mafia': (
                "**Instructions:**\n"
                "1. Use this chat to coordinate with your fellow mafia members\n"
                "2. During night phase, vote in the poll to choose who to eliminate\n"
                "3. Discuss with your team before voting\n"
                "4. During the day, act natural in the main chat and try not to get caught!\n"
            ),
            'detective': (
                "**Instructions:**\n"
                "1. During night phase, vote in the poll to choose who to investigate\n"
                "2. Share your findings with other detectives\n"
                "3. Be strategic about revealing information in the main chat\n"
                "4. Be careful, the mafia might kill one of us tonight\n"
            ),
            'doctor': (
                "**Instructions:**\n"
                "1. During night phase, vote in the poll to choose who to protect\n"
                "2. You can protect yourself, but choose wisely!\n"
                "3. Help keep the villagers alive!\n"
                "4. Consider protecting players who seem to have important information\n"
            )
        }
        
        # Cache static story prompts
        self.story_prompts = {
            'night': "Create a brief, spooky description of night falling on the village.",
            'mafia': "Create a brief, sinister message for the mafia members as they gather.",
            'detective': "Create a brief message for the detective investigating suspicious activities.",
            'doctor': "Create a brief message for the doctor protecting the villagers.",
            'day': "Create a scene describing the village coming to life as dawn breaks, with tension in the air as villagers gather to discuss the night's events."
        }
        
    def generate_npc_name(self) -> str:
        """Generate a random medieval-style name for an NPC"""
        first_names = ["Aldrich", "Bartholomew", "Constantine", "Darius", "Edmund", "Felix", "Galahad", "Henrik"]
        surnames = ["Blackwood", "Crowley", "Darkshire", "Elderworth", "Frostweaver", "Grimsworth", "Hawthorne"]
        return f"{random.choice(first_names)} {random.choice(surnames)}"

    async def add_npcs_if_needed(self):
        """Add NPC players if there aren't enough real players"""
        min_players = 4  # Minimum required for a balanced game
        real_players = len([p for p in self.players.keys() if p < self.npc_base_id])
        print(f"Adding NPCs. Current real players: {real_players}")
        
        while len(self.players) < min_players:
            npc_id = self.npc_base_id + self.npc_count
            npc_name = self.generate_npc_name()
            npc = NPCPlayer(npc_name, npc_id)
            self.players[npc_id] = npc
            self.npc_count += 1
            print(f"Added NPC: {npc_name} (ID: {npc_id})")
            
            if self.main_channel:
                try:
                    if self.storyteller:
                        # Generate introduction story for NPC
                        prompt = f"Create a brief one-sentence introduction for {npc_name}, a stranger who has joined the village."
                        story = await self.storyteller.generate_story(prompt)
                        await self.main_channel.send(story)
                    else:
                        await self.main_channel.send(f"{npc_name} has joined the village.")
                except Exception as e:
                    print(f"Error introducing NPC: {e}")
                    await self.main_channel.send(f"{npc_name} has joined the village.")
                    
    def assign_roles(self):
        """Assign roles to all players randomly"""
        print("Assigning roles to players...")
        
        # Get list of all players
        player_ids = list(self.players.keys())
        if len(player_ids) < 4:
            raise ValueError("Not enough players to assign roles")
            
        # Calculate number of each role based on player count
        num_players = len(player_ids)
        num_mafia = max(1, num_players // 4)  # At least 1 mafia
        
        # Create list of all roles
        roles = []
        roles.extend([PlayerRole.MAFIA] * num_mafia)  # Add mafia roles
        roles.append(PlayerRole.DETECTIVE)  # Add one detective
        roles.append(PlayerRole.DOCTOR)  # Add one doctor
        
        # Fill remaining slots with villagers
        num_villagers = num_players - len(roles)
        roles.extend([PlayerRole.VILLAGER] * num_villagers)
        
        # Shuffle both the player IDs and roles
        random.shuffle(player_ids)
        random.shuffle(roles)
        
        # Reset roles and assign new ones
        self.player_roles.clear()
        for player_id, role in zip(player_ids, roles):
            self.player_roles[player_id] = role
            player_name = self.players[player_id].name
            print(f"Assigned {role.value} to {player_name}")
        
        print("Player Roles:")
        for player_id, role in self.player_roles.items():
            player_name = self.players[player_id].name
            print(f"{player_name}: {role.value}")
        
        # Set initial alive players
        self.alive_players = player_ids.copy()
        self.dead_players = []

    async def send_role_dms(self):
        """Send role information to all players"""
        print("Sending role DMs...")
        
        # First, collect all mafia members for the mafia message
        mafia_members = [self.players[pid].name for pid, role in self.player_roles.items() 
                        if role == PlayerRole.MAFIA and pid < self.npc_base_id]
        
        for player_id, role in self.player_roles.items():
            if player_id >= self.npc_base_id:  # Skip NPCs
                continue
                
            try:
                player = self.players[player_id]
                role_msg = f"Your role is: {role.value}"
                
                # Add mafia member list for mafia players
                if role == PlayerRole.MAFIA and len(mafia_members) > 1:
                    other_mafia = [name for name in mafia_members if name != player.name]
                    if other_mafia:
                        role_msg += f"\nOther mafia members: {', '.join(other_mafia)}"
                
                await player.send(role_msg)
                print(f"Sent role DM to {player_id}")
            except Exception as e:
                print(f"Error sending role DM to {player_id}: {e}")
                
    async def process_night_actions(self):
        """Process all night actions and determine outcomes"""
        if not self.night_actions:
            await self.main_channel.send("No actions were taken during the night.")
            return

        # Process detective's investigation
        if 'investigate' in self.night_actions:
            target_id = self.night_actions['investigate']
            target_role = self.player_roles[target_id]
            target_player = self.players[target_id]
            if self.detective_channel:
                await self.detective_channel.send(f"🔍 Investigation results: **{target_player.name}** is a **{target_role.value}**!")

        # Process doctor's save
        saved_id = self.night_actions.get('save')
        
        # Process mafia's kill
        if 'kill' in self.night_actions:
            target_id = self.night_actions['kill']
            target_player = self.players[target_id]
            
            if target_id == saved_id:
                await self.main_channel.send(f"🏥 The Doctor successfully saved someone from death!")
            else:
                self.dead_players.append(target_id)
                await self.main_channel.send(f"💀 **{target_player.name}** was found dead! They were a **{self.player_roles[target_id].value}**.")
        
        # Clear night actions
        self.night_actions.clear()
        
        # Check win conditions
        await self.check_win_conditions()

    async def start_day_phase(self):
        """Start the day phase of the game"""
        self.state = GameState.DAY
        
        # Generate day transition story
        day_prompt = "Create a scene describing the village coming to life as dawn breaks, with tension in the air as villagers gather to discuss the night's events."
        day_story = await self.storyteller.generate_story(day_prompt)
        await self.main_channel.send(day_story)
        
        # Move to voting phase after a short discussion period
        await asyncio.sleep(30)
        await self.start_voting_phase()

    async def night_phase(self):
        """Start the night phase of the game"""
        self.state = GameState.NIGHT
        self.night_actions = {}  # Reset night actions
        
        # Start night phase timer immediately
        timer_task = asyncio.create_task(self._run_night_timer())
        
        # Get list of alive players for polls
        alive_player_objects = [self.players[pid] for pid in self.alive_players]
        status_msg = self.get_player_status_message()
        
        # Generate all stories concurrently
        story_tasks = {
            role: self.storyteller.generate_story(prompt, max_length=400)
            for role, prompt in self.story_prompts.items()
        }
        
        # Wait for all stories to complete
        stories = await asyncio.gather(*story_tasks.values())
        story_results = dict(zip(story_tasks.keys(), stories))
        
        # Send main channel message
        await self.main_channel.send(
            f"{story_results['night']}\n\n"
            f"Night falls... The village sleeps while some remain active in the shadows...\n\n"
            f"{status_msg}"
        )
        
        # Create all polls concurrently
        poll_tasks = []
        
        # Mafia channel setup
        if self.mafia_channel:
            mafia_msg = (
                f"🌙 **Night Phase Begins** 🌙\n\n"
                f"{story_results['mafia']}\n\n"
                f"{status_msg}\n\n"
                f"{self.role_instructions['mafia']}"
            )
            poll_tasks.append(asyncio.create_task(self.mafia_channel.send(mafia_msg)))
            poll_tasks.append(asyncio.create_task(
                self.create_action_poll(self.mafia_channel, "kill", alive_player_objects)
            ))
            
        # Detective channel setup
        if self.detective_channel:
            detective_msg = (
                f"🔍 **Night Phase Begins** 🔍\n\n"
                f"{story_results['detective']}\n\n"
                f"{status_msg}\n\n"
                f"{self.role_instructions['detective']}"
            )
            poll_tasks.append(asyncio.create_task(self.detective_channel.send(detective_msg)))
            poll_tasks.append(asyncio.create_task(
                self.create_action_poll(self.detective_channel, "investigate", alive_player_objects)
            ))
            
        # Doctor channel setup
        if self.doctor_channel:
            doctor_msg = (
                f"💉 **Night Phase Begins** 💉\n\n"
                f"{story_results['doctor']}\n\n"
                f"{status_msg}\n\n"
                f"{self.role_instructions['doctor']}"
            )
            poll_tasks.append(asyncio.create_task(self.doctor_channel.send(doctor_msg)))
            poll_tasks.append(asyncio.create_task(
                self.create_action_poll(self.doctor_channel, "save", alive_player_objects)
            ))
        
        # Wait for all messages and polls to be sent
        poll_results = await asyncio.gather(*poll_tasks)
        
        # Process polls (these need to be sequential due to game logic)
        polls = [r for r in poll_results if isinstance(r, discord.Message)]  # Filter out the sent messages
        for poll, action in zip(polls, ["kill", "investigate", "save"]):
            if poll:  # If poll was created successfully
                result = await self.get_poll_result(poll, len(alive_player_objects))
                if result is not None:
                    self.night_actions[action] = self.alive_players[result]
        
        # Process NPC actions immediately
        await self.process_npc_actions()
        
        # Wait for night timer to complete
        try:
            await timer_task
        except asyncio.CancelledError:
            pass  # Timer was cancelled, that's okay
        
        if self.state == GameState.NIGHT:  # Only proceed if still in night phase
            await self.process_night_actions()
            await self.day_phase()

    async def _run_night_timer(self):
        """Run the night phase timer"""
        # Send initial timer message
        await self.main_channel.send("The night phase will end in 60 seconds...")
        await asyncio.sleep(30)  # 30 seconds for night phase

    async def start_voting_phase(self):
        """Start the voting phase"""
        self.state = GameState.VOTING
        self.current_votes = {}
        
        # Get list of alive players for the poll
        alive_player_objects = [self.players[pid] for pid in self.alive_players]
        
        # Create voting poll message
        await self.main_channel.send("🗳️ **Voting Phase Begins** 🗳️\nThe village must decide who to eliminate!")
        
        # Create the poll for voting
        poll_msg = await self.create_action_poll(
            self.main_channel, 
            "eliminate", 
            alive_player_objects,
            timeout=30  # 30 seconds for voting
        )
        
        if poll_msg:
            # Wait for poll results
            result = await self.get_poll_result(poll_msg, len(alive_player_objects))
            
            if result is not None:
                eliminated_player_id = self.alive_players[result]
                await self.eliminate_player(eliminated_player_id)
            else:
                await self.main_channel.send("No consensus was reached. Nobody was eliminated.")
        
        # Move to night phase
        await self.night_phase()

    async def eliminate_player(self, player_id: int):
        """Eliminate a player from the game"""
        if player_id in self.alive_players:
            self.alive_players.remove(player_id)
            player = self.players[player_id]
            role = self.player_roles.get(player_id, "Villager")
            
            # Announce elimination
            await self.main_channel.send(
                f"🪦 The village has decided to eliminate **{player.display_name}**.\n"
                f"They were a **{role}**!"
            )
            
            # Check win conditions
            await self.check_win_condition()
            
    def get_player_status_message(self):
        """Get a formatted message showing alive and dead players"""
        alive_players = [self.players[pid].name for pid in self.alive_players]
        dead_players = [self.players[pid].name for pid in self.players.keys() if pid not in self.alive_players]
        
        msg = "**Player Status**\n"
        msg += "🟢 **Alive**: " + ", ".join(alive_players) + "\n"
        if dead_players:
            msg += "💀 **Dead**: " + ", ".join(dead_players)
        return msg

    async def check_win_conditions(self) -> bool:
        """Check if either faction has won"""
        mafia_count = sum(1 for pid in self.alive_players if self.player_roles[pid] == PlayerRole.MAFIA)
        villager_count = len(self.alive_players) - mafia_count
        
        if mafia_count == 0:
            # Village wins
            prompt = "Create an triumphant ending where the village successfully eliminated all mafia members and peace is restored."
            story = await self.storyteller.generate_story(prompt)
            await self.main_channel.send(f"{story}\n\n The Village has won! ")
            return True
        elif mafia_count >= villager_count:
            # Mafia wins
            prompt = "Create a dark ending where the mafia has gained control of the village, striking fear into the hearts of the remaining villagers."
            story = await self.storyteller.generate_story(prompt)
            await self.main_channel.send(f"{story}\n\n The Mafia has won! ")
            return True
        return False

    async def reset_game(self):
        """Reset the game state and clean up resources"""
        # Clean up mafia chat channel if it exists
        if self.mafia_channel:
            try:
                await self.mafia_channel.delete()
            except:
                pass
            self.mafia_channel = None
        
        # Reset all game state
        self.players.clear()
        self.player_roles.clear()
        self.dead_players = set()
        self.night_actions = {}
        self.votes = {}
        self.state = GameState.WAITING
        self.main_channel = None
        self.mafia_channel = None
        self.detective_channel = None
        self.doctor_channel = None
        self.villager_channel = None
        self.npc_base_id = 1000000

    async def remove_player(self, player_id: int) -> bool:
        """Remove a player from the game and handle necessary adjustments"""
        if player_id not in self.players:
            return False
            
        player = self.players[player_id]
        
        # Remove from all game states
        self.players.pop(player_id)
        self.player_roles.pop(player_id, None)
        if player_id in self.alive_players:
            self.alive_players.remove(player_id)
        if player_id in self.dead_players:
            self.dead_players.remove(player_id)
        self.current_votes = {k: v for k, v in self.current_votes.items() if k != player_id and v != player_id}
        self.night_actions = {k: v for k, v in self.night_actions.items() if k != player_id and v != player_id}
        
        # Remove from mafia chat if applicable
        if self.mafia_channel and self.player_roles.get(player_id) == PlayerRole.MAFIA:
            try:
                await self.mafia_channel.set_permissions(player, overwrite=None)
            except:
                pass
        
        # Generate quit story
        if self.state != GameState.WAITING:
            prompt = f"Create a dramatic description of {player.name}'s sudden and mysterious departure from the village."
            story = await self.storyteller.generate_story(prompt)
            if self.main_channel:
                await self.main_channel.send(story)
        
        # Check if game should end due to player count
        if self.state != GameState.WAITING and len(self.players) < 4:
            await self.main_channel.send("Not enough players remaining. The game must end.")
            await self.reset_game()
            return True
            
        # Check win conditions if game is in progress
        if self.state != GameState.WAITING:
            await self.check_win_conditions()
            
        return True

    async def get_npc_action(self, npc_id: int, action_type: str) -> Optional[int]:
        """Get an AI-generated action for an NPC"""
        if not self.storyteller:
            return random.choice([pid for pid in self.alive_players if pid != npc_id])
            
        npc = self.players[npc_id]
        npc_role = self.player_roles[npc_id]
        
        # Get list of valid targets
        targets = [pid for pid in self.alive_players if pid != npc_id]
        if not targets:
            return None
            
        target_names = [self.players[pid].name for pid in targets]
        
        # Create context for the AI
        context = f"You are {npc.name}, a {npc_role.value} in a game of Mafia. "
        
        if action_type == "vote":
            context += f"You must vote to eliminate one player who you suspect is in the mafia. The candidates are: {', '.join(target_names)}."
        elif action_type == "mafia_kill":
            context += f"As a mafia member, you must choose a villager to eliminate. The potential targets are: {', '.join(target_names)}."
        elif action_type == "investigate":
            context += f"As the detective, you must choose a player to investigate. The suspects are: {', '.join(target_names)}."
        elif action_type == "protect":
            context += f"As the doctor, you must choose a player to protect from the mafia. The potential targets are: {', '.join(target_names)}."
            
        try:
            # Create a mock discord message
            mock_message = type('MockMessage', (), {'content': context})()
            chosen_name = await self.storyteller.agent.run(mock_message)
            chosen_name = chosen_name.strip()
            
            # Find the closest matching player name
            best_match = None
            for pid in targets:
                if self.players[pid].name.lower() in chosen_name.lower():
                    best_match = pid
                    break
                    
            if best_match is not None:
                return best_match
            
            # Fallback to random choice if no match found
            return random.choice(targets) if targets else None
            
        except Exception as e:
            print(f"Error getting NPC action: {e}")
            # Return a random target as fallback
            return random.choice(targets) if targets else None

    async def process_npc_actions(self):
        """Process actions for all NPCs during appropriate game phases"""
        if self.state == GameState.NIGHT:
            for player_id, role in self.player_roles.items():
                if player_id >= self.npc_base_id and player_id in self.alive_players:
                    if role == PlayerRole.MAFIA:
                        target = await self.get_npc_action(player_id, "mafia_kill")
                        if target:
                            self.night_actions[player_id] = target
                    elif role == PlayerRole.DETECTIVE:
                        target = await self.get_npc_action(player_id, "investigate")
                        if target:
                            self.night_actions[player_id] = target
                    elif role == PlayerRole.DOCTOR:
                        target = await self.get_npc_action(player_id, "protect")
                        if target:
                            self.night_actions[player_id] = target
        
        elif self.state == GameState.VOTING:
            for player_id in self.alive_players:
                if player_id >= self.npc_base_id and player_id not in self.current_votes:
                    target = await self.get_npc_action(player_id, "vote")
                    if target:
                        self.current_votes[player_id] = target
                        
                        # Generate voting story for NPC
                        npc = self.players[player_id]
                        target_player = self.players[target]
                        prompt = f"Create a dramatic moment where {npc.name} accuses {target_player.name} of being in league with the mafia."
                        story = await self.storyteller.generate_story(prompt)
                        await self.main_channel.send(story)

    async def setup_channels(self, ctx):
        """Set up all necessary channels for the game"""
        # Delete existing category and channels if they exist
        existing_category = discord.utils.get(ctx.guild.categories, name='mafia-game-channels')
        if existing_category:
            for channel in existing_category.channels:
                await channel.delete()
            await existing_category.delete()

        # Create new category for game channels
        category = await ctx.guild.create_category('mafia-game-channels', 
            overwrites={
                ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
        )

        # Store channels for special roles
        mafia_channel = None
        detective_channel = None
        doctor_channel = None

        # Create channels for each role
        for role in [PlayerRole.MAFIA, PlayerRole.DETECTIVE, PlayerRole.DOCTOR, PlayerRole.VILLAGER]:
            channel_name = f"{role.value.lower()}-chat"
            
            # Create channel with default permissions
            channel = await ctx.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites={
                    ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                    ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
            )

            # Find all players with this role
            members = []
            for player_id, player_role in self.player_roles.items():
                if player_role == role and player_id < self.npc_base_id:  # Skip NPCs
                    player = self.players[player_id]
                    if isinstance(player, discord.Member):
                        members.append(player)

            # Store channels for special roles
            if role == PlayerRole.MAFIA:
                mafia_channel = channel
            elif role == PlayerRole.DETECTIVE:
                detective_channel = channel
            elif role == PlayerRole.DOCTOR:
                doctor_channel = channel

            # Grant access to all players with this role
            for member in members:
                await channel.set_permissions(member, read_messages=True, send_messages=True)

            # Only send welcome message if there are members
            if members:
                if role == PlayerRole.MAFIA:
                    mafia_msg = "🎭 **Welcome to the Mafia Chat!** 🎭\n\n"
                    mafia_msg += f"**Members:** {', '.join(member.name for member in members)}\n\n"
                    mafia_msg += "**Instructions:**\n"
                    mafia_msg += "1. Use this chat to coordinate with your fellow mafia members\n"
                    mafia_msg += "2. During night phase, vote in the poll to choose who to eliminate\n"
                    mafia_msg += "3. Discuss with your team before voting\n"
                    mafia_msg += "4. During the day, act natural in the main chat and try not to get caught!\n"
                elif role == PlayerRole.DETECTIVE:
                    detective_msg = "🔍 **Welcome to the Detective Chat!** 🔍\n\n"
                    detective_msg += f"**Members:** {', '.join(member.name for member in members)}\n\n"
                    detective_msg += "**Instructions:**\n"
                    detective_msg += "1. Use the detective-chat channel to coordinate with other detectives\n"
                    detective_msg += "2. During night phase, vote in the poll to choose who to investigate\n"
                    detective_msg += "3. Share your findings with other detectives\n"
                    detective_msg += "4. Be strategic about revealing information in the main chat\n"
                elif role == PlayerRole.DOCTOR:
                    doctor_msg = "💉 **Welcome to the Doctor Chat!** 💉\n\n"
                    doctor_msg += f"**Members:** {', '.join(member.name for member in members)}\n\n"
                    doctor_msg += "**Instructions:**\n"
                    doctor_msg += "1. Use the doctor-chat channel to coordinate with other doctors\n"
                    doctor_msg += "2. During night phase, vote in the poll to choose who to protect\n"
                    doctor_msg += "3. You can protect yourself, but choose wisely!\n"
                    doctor_msg += "4. Consider protecting players who seem to have important information\n"
                elif role == PlayerRole.VILLAGER:
                    villager_msg = "🏠 **Welcome to the Villager Chat!** 🏠\n\n"
                    villager_msg += f"**Members:** {', '.join(member.name for member in members)}\n\n"
                    villager_msg += "**Instructions:**\n"
                    villager_msg += "1. Use the villager-chat channel to coordinate with fellow villagers\n"
                    villager_msg += "2. During the day, discuss who might be the mafia\n"
                    villager_msg += "3. Vote wisely during the voting phase!\n"
                    villager_msg += "4. Pay attention to player behavior and voting patterns\n"
                await channel.send(villager_msg)

        # Store special role channels
        self.mafia_channel = mafia_channel
        self.detective_channel = detective_channel
        self.doctor_channel = doctor_channel

        # Create individual private channels for each player
        for player_id, role in self.player_roles.items():
            if player_id >= self.npc_base_id:  # Skip NPCs
                continue

            player = self.players[player_id]
            if not isinstance(player, discord.Member):  # Skip if not a real Discord member
                continue

            # Create private channel name
            channel_name = f"private-{player.name.lower()}"

            # Create private channel with proper permissions
            channel = await ctx.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites={
                    ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
                    ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    player: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }
            )

            # Send private welcome message
            private_msg = f"🎭 **Welcome {player.name}!** 🎭\n\n"
            private_msg += f"Your role is: **{role.value}**\n\n"
            private_msg += "This is your private channel. You will receive important game updates here.\n\n"

            if role == PlayerRole.MAFIA:
                private_msg += "**Instructions:**\n"
                private_msg += "1. Use the mafia-chat channel to coordinate with your team\n"
                private_msg += "2. During night phase, vote in the poll to choose who to eliminate\n"
                private_msg += "3. Discuss with your team before voting\n"
                private_msg += "4. During the day, act natural in the main chat and try not to get caught!\n"
            elif role == PlayerRole.DETECTIVE:
                private_msg += "**Instructions:**\n"
                private_msg += "1. Use the detective-chat channel to coordinate with other detectives\n"
                private_msg += "2. During night phase, vote in the poll to choose who to investigate\n"
                private_msg += "3. Share your findings with other detectives\n"
                private_msg += "4. Be strategic about revealing information in the main chat\n"
            elif role == PlayerRole.DOCTOR:
                private_msg += "**Instructions:**\n"
                private_msg += "1. Use the doctor-chat channel to coordinate with other doctors\n"
                private_msg += "2. During night phase, vote in the poll to choose who to protect\n"
                private_msg += "3. You can protect yourself, but choose wisely!\n"
                private_msg += "4. Consider protecting players who seem to have important information\n"
            elif role == PlayerRole.VILLAGER:
                private_msg += "**Instructions:**\n"
                private_msg += "1. Use the villager-chat channel to coordinate with fellow villagers\n"
                private_msg += "2. During the day, discuss who might be the mafia\n"
                private_msg += "3. Vote wisely during the voting phase!\n"
                private_msg += "4. Pay attention to player behavior and voting patterns\n"
            await channel.send(private_msg)

    async def begin_game(self, ctx):
        """Begin the game with current players"""
        if self.state != GameState.WAITING:
            await ctx.send("A game is already in progress!")
            return

        if len(self.players) < 1:  # Allow starting with at least 1 real player
            await ctx.send("Not enough players to start! Need at least 1 player.")
            return

        try:
            # Store the main channel
            self.main_channel = ctx.channel
            await ctx.send("Starting game setup...")

            # Add NPCs if needed
            await ctx.send("Adding NPCs if needed...")
            await self.add_npcs_if_needed()

            if len(self.players) < 4:
                await ctx.send("Error: Could not add enough NPCs to start the game.")
                self.state = GameState.WAITING
                return

            # Initialize alive players list
            self.alive_players = list(self.players.keys())
            await ctx.send(f"Players in game: {len(self.alive_players)}")

            # Generate game start story
            await ctx.send("Generating game start story...")
            start_story = "A small village gathers as night approaches, unaware of the danger that lurks within..."
            try:
                start_prompt = "Create a brief introduction to a small village that's about to be infiltrated by the mafia."
                story = await self.storyteller.generate_story(start_prompt)
                if story:
                    start_story = story
            except Exception as e:
                print(f"Error generating start story: {e}")
            await ctx.send(start_story)

            # Assign roles
            await ctx.send("Assigning roles...")
            self.assign_roles()

            # Set up channels
            await ctx.send("Setting up channels...")
            await self.setup_channels(ctx)

            self.state = GameState.NIGHT

        except Exception as e:
            print(f"Error setting up game: {e}")
            await ctx.send(f"Error setting up game: {str(e)}")
            self.state = GameState.WAITING
            return

        await ctx.send("Starting night phase...")
        await self.night_phase()

    async def create_action_poll(self, channel, action_type, alive_players):
        """Create a poll for role actions using Discord's SelectMenu"""
        if not alive_players:
            print(f"No alive players for {action_type} poll")
            return None

        action_text = {
            "kill": "Who would you like to eliminate?",
            "investigate": "Who would you like to investigate?",
            "save": "Who would you like to protect?"
        }
        
        try:
            # Create select options from alive players
            options = [
                discord.SelectOption(
                    label=player.name,
                    value=str(i)  # Use index as value for easy retrieval
                )
                for i, player in enumerate(alive_players)
            ]
            
            # Create the view with select menu
            view = PollView(timeout=30)
            view.select_callback.options = options
            
            # Send the message with the select menu
            poll = await channel.send(
                content=f"**{action_text[action_type]}**\nYou have 30 seconds to vote.",
                view=view
            )
            
            # Store the view in our tracking dict
            self.active_polls[poll.id] = view
            return poll
            
        except Exception as e:
            print(f"Error creating poll in {channel.name}: {e}")
            return None

    async def get_poll_result(self, poll_message, num_options):
        """Get the result of a poll after waiting for votes"""
        # Wait for votes
        await asyncio.sleep(30)
        
        try:
            # Get the view from our tracking dict
            view = self.active_polls.get(poll_message.id)
            
            if not view or not view.votes:
                # No votes cast, choose randomly
                return random.randrange(num_options)
            
            # Count votes for each option
            vote_counts = [0] * num_options
            for vote in view.votes.values():
                vote_counts[vote] += 1
            
            # Find options with max votes
            max_votes = max(vote_counts)
            tied_indices = [i for i, count in enumerate(vote_counts) if count == max_votes]
            
            # Randomly choose if there's a tie
            chosen_index = random.choice(tied_indices)
            
            # Disable the select menu
            for item in view.children:
                item.disabled = True
            await poll_message.edit(view=view)
            
            # Clean up the poll from our tracking
            self.active_polls.pop(poll_message.id, None)
            
            return chosen_index
            
        except Exception as e:
            print(f"Error getting poll results: {e}")
            # Clean up the poll from our tracking
            self.active_polls.pop(poll_message.id, None)
            # In case of error, return a random choice
            return random.randrange(num_options)

    async def cleanup_channels(self):
        """Clean up role-specific channels"""
        try:
            if self.mafia_channel:
                await self.mafia_channel.delete()
                self.mafia_channel = None
                
            if self.detective_channel:
                await self.detective_channel.delete()
                self.detective_channel = None
                
            if self.doctor_channel:
                await self.doctor_channel.delete()
                self.doctor_channel = None
        except discord.Forbidden:
            # If we don't have permission to delete channels
            print("Warning: Unable to delete some role channels due to permissions")
        except Exception as e:
            print(f"Error cleaning up channels: {e}")

    def reset_game_state(self):
        """Reset all game state variables"""
        # Reset all game state
        self.players.clear()
        self.player_roles.clear()
        self.alive_players.clear()
        self.current_votes.clear()
        self.night_actions.clear()
        self.dead_players.clear()
        self.active_polls.clear()  # Clear any active polls
        self.state = GameState.WAITING

    async def day_phase(self):
        """Start the day phase of the game"""
        self.state = GameState.DAY
        
        # Generate day story
        day_story = await self.storyteller.generate_story(self.story_prompts['day'])
        
        # Get status update
        status_msg = self.get_player_status_message()
        
        # Send day phase message
        await self.main_channel.send(
            f"☀️ **Day Phase Begins** ☀️\n\n"
            f"{day_story}\n\n"
            f"The village awakens to discuss the night's events...\n\n"
            f"{status_msg}"
        )
        
        # Start voting phase after discussion period
        await self.main_channel.send("The village will have 60 seconds for discussion before voting begins...")
        await asyncio.sleep(60)  # 60 seconds for discussion
        
        if self.state == GameState.DAY:  # Only proceed if still in day phase
            await self.start_voting_phase()

@bot.command(name='vote')
async def vote(ctx):
    """Legacy vote command - now redirects to the poll system"""
    if ctx.guild is None:
        return
        
    game = games.get(ctx.guild.id)
    if not game or game.state != GameState.VOTING:
        return
        
    await ctx.send("Please use the poll above to cast your vote! ⬆️")

@bot.command(name='kill')
async def kill_command(ctx, target: str = None):
    """Command for mafia to kill a player during the night phase"""
    global current_game
    
    # Basic validation
    if not current_game:
        await ctx.send("No game is currently running!")
        return
        
    if ctx.channel != current_game.mafia_channel:
        if isinstance(ctx.channel, discord.TextChannel):  # Only respond in guild channels, not DMs
            await ctx.send("The kill command can only be used in the mafia chat!")
        return
        
    if ctx.author.id not in current_game.player_roles or current_game.player_roles[ctx.author.id] != PlayerRole.MAFIA:
        await ctx.send("Only mafia members can use the kill command!")
        return
        
    if current_game.state != GameState.NIGHT:
        await ctx.send("You can only kill during the night phase!")
        return
        
    if target is None:
        status_msg = current_game.get_player_status_message()
        await ctx.send(f"Please specify a target using `!kill <player name>`\n\n{status_msg}")
        return
    
    # Find target player by name (case insensitive)
    target_player = None
    target = target.lower()
    for player_id, player in current_game.players.items():
        if player.name.lower() == target:
            target_player = player
            break
    
    if not target_player or target_player.id not in current_game.alive_players:
        status_msg = current_game.get_player_status_message()
        await ctx.send(f"Invalid target! Please specify a living player's name.\n\n{status_msg}")
        return
    
    current_game.night_actions[ctx.author.id] = target_player.id
    await ctx.send(f"🔪 You have chosen to kill {target_player.name}. The kill will be processed when night ends.")

@bot.command(name='investigate')
async def investigate_command(ctx, target: str = None):
    """Command for detective to investigate a player during the night phase"""
    global current_game
    
    # Basic validation
    if not current_game:
        await ctx.send("No game is currently running!")
        return
        
    if ctx.author.id not in current_game.player_roles or current_game.player_roles[ctx.author.id] != PlayerRole.DETECTIVE:
        await ctx.send("Only the detective can use the investigate command!")
        return
        
    if current_game.state != GameState.NIGHT:
        await ctx.send("You can only investigate during the night phase!")
        return
        
    if target is None:
        status_msg = current_game.get_player_status_message()
        await ctx.send(f"Please specify a target using `!investigate <player name>`\n\n{status_msg}")
        return
    
    # Find target player by name (case insensitive)
    target_player = None
    target = target.lower()
    for player_id, player in current_game.players.items():
        if player.name.lower() == target:
            target_player = player
            break
    
    if not target_player or target_player.id not in current_game.alive_players:
        status_msg = current_game.get_player_status_message()
        await ctx.send(f"Invalid target! Please specify a living player's name.\n\n{status_msg}")
        return
    
    current_game.night_actions[ctx.author.id] = target_player.id
    await ctx.send(f"🔍 You have chosen to investigate {target_player.name}. The results will be sent when night ends.")

@bot.command(name='save')
async def save_command(ctx, target: str = None):
    """Command for doctor to save a player during the night phase"""
    global current_game
    
    # Basic validation
    if not current_game:
        await ctx.send("No game is currently running!")
        return
        
    if ctx.author.id not in current_game.player_roles or current_game.player_roles[ctx.author.id] != PlayerRole.DOCTOR:
        await ctx.send("Only the doctor can use the save command!")
        return
        
    if current_game.state != GameState.NIGHT:
        await ctx.send("You can only save during the night phase!")
        return
        
    if target is None:
        status_msg = current_game.get_player_status_message()
        await ctx.send(f"Please specify a target using `!save <player name>`\n\n{status_msg}")
        return
    
    # Find target player by name (case insensitive)
    target_player = None
    target = target.lower()
    for player_id, player in current_game.players.items():
        if player.name.lower() == target:
            target_player = player
            break
    
    if not target_player or target_player.id not in current_game.alive_players:
        status_msg = current_game.get_player_status_message()
        await ctx.send(f"Invalid target! Please specify a living player's name.\n\n{status_msg}")
        return
    
    current_game.night_actions[ctx.author.id] = target_player.id
    await ctx.send(f"💉 You have chosen to protect {target_player.name}. They will be saved if the mafia targets them tonight.")

@bot.command(name='endgame')
async def end_game(ctx):
    """End the current game"""
    global current_game
    
    if current_game is None:
        await ctx.send("No game is currently running!")
        return
        
    # Clean up channels first
    await current_game.cleanup_channels()
    
    # Reset game state
    current_game.reset_game_state()
    
    await ctx.send("Game ended. All channels have been cleaned up.")

# Global game instance
current_game = None

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')

@bot.command(name='startgame')
async def start_game(ctx):
    """Start a new game of Mafia"""
    global current_game
    
    if current_game is not None and current_game.state != GameState.WAITING:
        await ctx.send("A game is already in progress!")
        return
        
    current_game = MafiaGame()
    await ctx.send("Game started! Use !join to join the game and !begin when ready to start.")

@bot.command(name='join')
async def join_game(ctx):
    """Join the current game"""
    global current_game
    
    if current_game is None:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    if current_game.state != GameState.WAITING:
        await ctx.send("Cannot join a game that has already started!")
        return
        
    if ctx.author.id in current_game.players:
        await ctx.send("You have already joined the game!")
        return
        
    current_game.players[ctx.author.id] = ctx.author
    await ctx.send(f"{ctx.author.name} has joined the game!")

@bot.command(name='begin')
async def begin_game(ctx):
    """Begin the game with current players"""
    global current_game
    
    if current_game is None:
        await ctx.send("No game is currently running! Use !startgame to start a new game.")
        return
        
    await current_game.begin_game(ctx)

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
