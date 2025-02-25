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
        
    async def generate_story(self, prompt: str) -> str:
        """Generate a story using Mistral AI"""
        try:
            # Create a mock discord message for the agent
            mock_message = type('MockMessage', (), {'content': prompt})()
            response = await self.agent.run(mock_message)
            
            if not response:
                return "The village continues its story..."  # Fallback if empty response
            
            # If response is too long, ask Mistral to shorten it
            if len(response) > 1900:  # Leave buffer for safety
                shorten_prompt = f"Please shorten this story while keeping the main dramatic elements (keep it under 1900 characters):\n\n{response}"
                mock_message.content = shorten_prompt
                response = await self.agent.run(mock_message)
                
                if not response or len(response) > 1900:
                    # If still too long or empty, use first 1900 chars as fallback
                    return response[:1900]
                    
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
        print("StoryTeller initialized successfully")

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
        
        # Send night phase message to main channel
        night_prompt = "Create a spooky description of night falling on the village."
        night_story = await self.storyteller.generate_story(night_prompt)
        status_msg = self.get_player_status_message()
        
        # Send voting phase message with player status
        await self.main_channel.send(f"{night_story}\n\nNight falls... The village sleeps while some remain active in the shadows...\n\n{status_msg}")
        
        # Get list of alive players for polls
        alive_player_objects = [self.players[pid] for pid in self.alive_players]
        
        # Send role-specific messages and create polls
        mafia_prompt = "Create a sinister message for the mafia members as they gather to choose their next victim."
        mafia_story = await self.storyteller.generate_story(mafia_prompt)
        
        # Send message to mafia chat first
        if self.mafia_channel:
            mafia_msg = f"🌙 **Night Phase Begins** 🌙\n\n"
            mafia_msg += f"{mafia_story}\n\n"
            mafia_msg += f"{status_msg}\n\n"
            mafia_msg += "**Instructions:**\n"
            mafia_msg += "1. Use this chat to coordinate with your fellow mafia members\n"
            mafia_msg += "2. During night phase, vote in the poll to choose who to eliminate\n"
            mafia_msg += "3. Discuss with your team before voting\n"
            mafia_msg += "4. During the day, act natural in the main chat and try not to get caught!\n"
            await self.mafia_channel.send(mafia_msg)
            
            # Create kill poll for mafia
            kill_poll = await self.create_action_poll(self.mafia_channel, "kill", alive_player_objects)
            if kill_poll:
                kill_result = await self.get_poll_result(kill_poll, len(alive_player_objects))
                self.night_actions['kill'] = self.alive_players[kill_result]

        # Send messages to detective and doctor chats
        if self.detective_channel:
            detective_prompt = "Create a message for the detective as they prepare to investigate suspicious activities in the village."
            detective_story = await self.storyteller.generate_story(detective_prompt)
            detective_msg = f"🔍 **Night Phase Begins** 🔍\n\n"
            detective_msg += f"{detective_story}\n\n"
            detective_msg += f"{status_msg}\n\n"
            detective_msg += "**Instructions:**\n"
            detective_msg += "1. During night phase, vote in the poll to choose who to investigate\n"
            detective_msg += "2. Share your findings with other detectives\n"
            detective_msg += "3. Be strategic about revealing information in the main chat\n"
            detective_msg += "4. Be careful, the mafia might kill one of us tonight\n"
            await self.detective_channel.send(detective_msg)
            
            # Create investigation poll for detective
            investigate_poll = await self.create_action_poll(self.detective_channel, "investigate", alive_player_objects)
            if investigate_poll:
                investigate_result = await self.get_poll_result(investigate_poll, len(alive_player_objects))
                self.night_actions['investigate'] = self.alive_players[investigate_result]

        if self.doctor_channel:
            doctor_prompt = "Create a message for the doctor as they prepare to protect the villagers from harm."
            doctor_story = await self.storyteller.generate_story(doctor_prompt)
            doctor_msg = f"💉 **Night Phase Begins** 💉\n\n"
            doctor_msg += f"{doctor_story}\n\n"
            doctor_msg += f"{status_msg}\n\n"
            doctor_msg += "**Instructions:**\n"
            doctor_msg += "1. During night phase, vote in the poll to choose who to protect\n"
            doctor_msg += "2. You can protect yourself, but choose wisely!\n"
            doctor_msg += "3. Help keep the villagers alive!\n"
            doctor_msg += "4. Consider protecting players who seem to have important information\n"
            await self.doctor_channel.send(doctor_msg)
            
            # Create save poll for doctor
            save_poll = await self.create_action_poll(self.doctor_channel, "save", alive_player_objects)
            if save_poll:
                save_result = await self.get_poll_result(save_poll, len(alive_player_objects))
                self.night_actions['save'] = self.alive_players[save_result]
        
        # Process NPC actions immediately
        await self.process_npc_actions()
        
        # Send reminder message
        await self.main_channel.send("The night phase will end in 60 seconds...")
        
        # Start night phase timer
        await asyncio.sleep(60)  # 60 seconds for night phase
        if self.state == GameState.NIGHT:  # Only proceed if still in night phase
            await self.process_night_actions()
            await self.day_phase()

    async def start_voting_phase(self):
        """Start the voting phase"""
        self.state = GameState.VOTING
        self.current_votes = {}
        
        # Generate voting story
        voting_prompt = "Create a tense description of the villagers gathering to decide who among them might be working with the mafia."
        voting_story = await self.storyteller.generate_story(voting_prompt)
        status_msg = self.get_player_status_message()
        
        # Send voting phase message with player status
        await self.main_channel.send(f"🗳️ **Voting Phase Begins** 🗳️\n\n{voting_story}\n\n{status_msg}\n\nUse `!vote <player name>` to cast your vote!")

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
        self.state = GameState.WAITING
        self.alive_players.clear()
        self.current_votes.clear()
        self.night_actions.clear()
        self.dead_players.clear()
        
        if self.main_channel:
            await self.main_channel.send("The game has been reset. Start a new game with !startgame")
            
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
                    mafia_msg += "4. During the day, act natural in the main chat and try not to get caught!\n\n"
                    mafia_msg += "**Example Messages:**\n"
                    mafia_msg += "- 'I think we should target [player] tonight, they're getting suspicious of us'\n"
                    mafia_msg += "- 'Let's vote for [player] during the day to avoid suspicion'\n"
                    mafia_msg += "- 'Be careful, the detective might investigate one of us tonight'\n\n"
                    mafia_msg += "**Strategic Tips:**\n"
                    mafia_msg += "- Try to blend in with the villagers during the day\n"
                    mafia_msg += "- Use your night actions wisely to eliminate threats\n"
                    mafia_msg += "- Coordinate with your fellow mafia members to achieve your goals\n"
                    await mafia_channel.send(mafia_msg)
                elif role == PlayerRole.DETECTIVE:
                    detective_msg = "🔍 **Welcome to the Detective Chat!** 🔍\n\n"
                    detective_msg += f"**Members:** {', '.join(member.name for member in members)}\n\n"
                    detective_msg += "**Instructions:**\n"
                    detective_msg += "1. During night phase, vote in the poll to choose who to investigate\n"
                    detective_msg += "2. Share your findings with other detectives\n"
                    detective_msg += "3. Work together to identify the mafia members!\n"
                    detective_msg += "4. Be strategic about revealing information in the main chat\n\n"
                    detective_msg += "**Example Messages:**\n"
                    detective_msg += "- 'I investigated [player] last night, they're definitely suspicious'\n"
                    detective_msg += "- 'Let's coordinate our investigations to cover more ground'\n"
                    detective_msg += "- 'Should we reveal what we found about [player] to the village?'\n\n"
                    detective_msg += "**Strategic Tips:**\n"
                    detective_msg += "- Use your investigations to gather information about suspicious players\n"
                    detective_msg += "- Share your findings with other detectives to build a case\n"
                    detective_msg += "- Be careful not to reveal too much information to the mafia\n"
                    await detective_channel.send(detective_msg)
                elif role == PlayerRole.DOCTOR:
                    doctor_msg = "💉 **Welcome to the Doctor Chat!** 💉\n\n"
                    doctor_msg += f"**Members:** {', '.join(member.name for member in members)}\n\n"
                    doctor_msg += "**Instructions:**\n"
                    doctor_msg += "1. During night phase, vote in the poll to choose who to protect\n"
                    doctor_msg += "2. You can protect yourself, but choose wisely!\n"
                    doctor_msg += "3. Help keep the villagers alive!\n"
                    doctor_msg += "4. Consider protecting players who seem to have important information\n\n"
                    doctor_msg += "**Example Messages:**\n"
                    doctor_msg += "- 'I think we should protect [player], they might be the detective'\n"
                    doctor_msg += "- 'The mafia might target [player] tonight, they're being very vocal'\n"
                    doctor_msg += "- 'Let's coordinate our saves to protect more villagers'\n\n"
                    doctor_msg += "**Strategic Tips:**\n"
                    doctor_msg += "- Use your saves to protect key players and prevent mafia kills\n"
                    doctor_msg += "- Consider protecting yourself if you're in danger\n"
                    doctor_msg += "- Coordinate with other doctors to maximize your impact\n"
                    await doctor_channel.send(doctor_msg)
                else:  # Villager
                    villager_msg = "🏠 **Welcome to the Villager Chat!** 🏠\n\n"
                    villager_msg += f"**Members:** {', '.join(member.name for member in members)}\n\n"
                    villager_msg += "**Instructions:**\n"
                    villager_msg += "1. Use this chat to coordinate with your fellow villagers\n"
                    villager_msg += "2. During the day, discuss who might be the mafia\n"
                    villager_msg += "3. Vote wisely during the voting phase!\n"
                    villager_msg += "4. Pay attention to player behavior and voting patterns\n\n"
                    villager_msg += "**Example Messages:**\n"
                    villager_msg += "- 'Did anyone notice how [player] voted yesterday?'\n"
                    villager_msg += "- 'I think [player] might be the detective, we should protect them'\n"
                    villager_msg += "- 'Let's coordinate our votes to eliminate the most suspicious player'\n\n"
                    villager_msg += "**Strategic Tips:**\n"
                    villager_msg += "- Pay attention to player behavior and voting patterns to identify suspicious players\n"
                    villager_msg += "- Use your votes to eliminate threats and protect the village\n"
                    villager_msg += "- Coordinate with other villagers to achieve your goals\n"
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
        """Create a poll for role actions"""
        if not alive_players:
            print(f"No alive players for {action_type} poll")
            return None

        action_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        
        # Create poll message
        action_text = {
            "kill": "Who would you like to eliminate?",
            "investigate": "Who would you like to investigate?",
            "save": "Who would you like to protect?"
        }
        
        poll_msg = f"**{action_text[action_type]}**\n\n"
        for i, player in enumerate(alive_players):
            poll_msg += f"{action_emojis[i]} {player.name}\n"
        
        try:
            # Send poll and add reactions
            message = await channel.send(poll_msg)
            for i in range(len(alive_players)):
                await message.add_reaction(action_emojis[i])
            return message
        except Exception as e:
            print(f"Error creating poll in {channel.name}: {e}")
            return None

    async def get_poll_result(self, poll_message, num_options):
        """Get the result of a poll after waiting for votes"""
        await asyncio.sleep(30)  # Wait 30 seconds for votes
        
        # Fetch updated message to get latest reactions
        message = await poll_message.channel.fetch_message(poll_message.id)
        
        # Count votes
        vote_counts = []
        for i in range(num_options):
            reaction = message.reactions[i]
            # Subtract 1 from count to exclude bot's reaction
            vote_counts.append(reaction.count - 1)
            
        # Find options with max votes
        max_votes = max(vote_counts)
        tied_indices = [i for i, count in enumerate(vote_counts) if count == max_votes]
        
        # Randomly choose if there's a tie
        chosen_index = random.choice(tied_indices)
        return chosen_index

    async def day_phase(self):
        """Handle the day phase of the game"""
        if self.state != GameState.DAY:
            return
            
        # Clear previous night's actions
        self.night_actions.clear()
        
        # Check win conditions
        if await self.check_win_conditions():
            return
            
        status_msg = self.get_player_status_message()
        await self.main_channel.send(f"**It's time to discuss and vote!**\n\n{status_msg}\n\nUse `!vote <player_name>` to vote for someone to eliminate.")
        
        # Move to voting phase
        self.state = GameState.VOTING
        self.current_votes.clear()
        
        # Set a timer for voting (5 minutes)
        await asyncio.sleep(300)
        
        # If voting hasn't ended naturally, end it now
        if self.state == GameState.VOTING:
            await self.end_voting()

    async def cleanup_channels(self):
        """Clean up all game-related channels"""
        if not self.main_channel:
            return
            
        # Find the game category
        category = discord.utils.get(self.main_channel.guild.categories, name='mafia-game-channels')
        if category:
            # Delete all channels in the category
            for channel in category.channels:
                try:
                    await channel.delete()
                except discord.errors.NotFound:
                    pass  # Channel was already deleted
                except Exception as e:
                    print(f"Error deleting channel {channel.name}: {e}")
                    
            # Delete the category itself
            try:
                await category.delete()
            except Exception as e:
                print(f"Error deleting category: {e}")

    def reset_game_state(self):
        """Reset all game state variables"""
        self.players = {}
        self.player_roles = {}
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

    async def end_voting(self):
        """End the voting phase and process results"""
        if self.state != GameState.VOTING:
            return
            
        # Count votes
        vote_counts = {}
        for voted_id in self.current_votes.values():
            vote_counts[voted_id] = vote_counts.get(voted_id, 0) + 1
            
        if not vote_counts:
            await self.main_channel.send("No one voted! Skipping elimination.")
        else:
            # Find player(s) with most votes
            max_votes = max(vote_counts.values())
            voted_players = [pid for pid, count in vote_counts.items() if count == max_votes]
            
            # Handle tie by random selection
            eliminated_id = random.choice(voted_players)
            eliminated_player = self.players[eliminated_id]
            eliminated_role = self.player_roles[eliminated_id]
            
            # Remove player
            self.dead_players.append(eliminated_id)
            await self.main_channel.send(f"🗳️ The votes are in! **{eliminated_player.name}** has been eliminated.\nThey were a **{eliminated_role.value}**!")
        
        # Check win conditions
        if await self.check_win_conditions():
            return
            
        # Move to night phase
        self.state = GameState.NIGHT
        await self.night_phase()

@bot.command(name='vote')
async def vote(ctx, target: Optional[str] = None):
    """Vote for a player during the day phase"""
    global current_game
    
    if current_game is None or current_game.state != GameState.VOTING:
        await ctx.send("No voting is currently in progress!")
        return
        
    if ctx.author.id not in current_game.alive_players:
        await ctx.send("Only living players can vote!")
        return
        
    if target is None:
        # Show current votes
        vote_counts = {}
        for voted_id in current_game.current_votes.values():
            vote_counts[voted_id] = vote_counts.get(voted_id, 0) + 1
            
        if not vote_counts:
            await ctx.send("No votes have been cast yet!")
            return
            
        vote_msg = "Current votes:\n"
        for voted_id, count in vote_counts.items():
            voted_name = current_game.players[voted_id].name
            vote_msg += f"{voted_name}: {count} votes\n"
        await ctx.send(vote_msg)
        return
    
    # Find target player by name (case insensitive)
    target_player = None
    target = target.lower()
    for player_id, player in current_game.players.items():
        if player.name.lower() == target:
            target_player = player
            break
    
    if not target_player or target_player.id not in current_game.alive_players:
        await ctx.send("Invalid target! Please specify a living player's name.")
        # List available players
        alive_players = [current_game.players[pid].name for pid in current_game.alive_players]
        await ctx.send(f"Living players: {', '.join(alive_players)}")
        return
        
    current_game.current_votes[ctx.author.id] = target_player.id
    await ctx.send(f"{ctx.author.name} voted for {target_player.name}!")
    
    # Check if everyone has voted
    living_players = len(current_game.alive_players)
    if len(current_game.current_votes) >= living_players:
        await current_game.end_voting()

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
